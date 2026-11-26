"""supervisor.py — Runtime supervisor, circuit breaker, canary functional probe, and status events."""

from __future__ import annotations

import asyncio
from collections import deque
import logging
from typing import Any, Literal
import statistics

from slm_pipeline.config import BreakerConfig
from slm_pipeline.contracts import (
    Clock,
    EventSink,
    FsmSnapshot,
    LlmClient,
    LlmError,
    SlmStatus,
)
from slm_pipeline.prompt import PromptBuilder
from slm_pipeline.scorer import LabelScorer

_LOGGER = logging.getLogger(__name__)


class SlmSupervisor:
    """Manages inference health via circuit breaker and functional canary probes."""

    def __init__(
        self,
        cfg: BreakerConfig,
        client: LlmClient,
        prompt_builder: PromptBuilder,
        scorer: LabelScorer,
        sink: EventSink,
        clock: Clock,
        enabled: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._prompt_builder = prompt_builder
        self._scorer = scorer
        self._sink = sink
        self._clock = clock
        self._enabled = enabled
        self._logger = logger or _LOGGER

        self._breaker: Literal["CLOSED", "OPEN", "HALF_OPEN"] = "CLOSED"
        self._state: Literal["UP", "DEGRADED", "DOWN", "DISABLED"] = "UP" if enabled else "DISABLED"
        self._reason: str = "INITIALIZED" if enabled else "FLAG_OFF"
        self._since_mono_ns: int = clock.now_mono_ns()

        self._consecutive_failures: int = 0
        self._current_open_s: float = cfg.open_s_initial
        self._recent_latencies: deque[float] = deque(maxlen=20)
        self._loop_task: asyncio.Task[None] | None = None
        self._running: bool = False

    @property
    def status(self) -> SlmStatus:
        return SlmStatus(
            state=self._state,
            reason=self._reason,
            since_mono_ns=self._since_mono_ns,
            breaker=self._breaker,
        )

    @property
    def breaker(self) -> Literal["CLOSED", "OPEN", "HALF_OPEN"]:
        return self._breaker

    def set_disabled(self, reason: str) -> None:
        """Mark supervisor permanently DISABLED (e.g. startup self-check failure)."""
        self._state = "DISABLED"
        self._reason = reason
        self._since_mono_ns = self._clock.now_mono_ns()
        self._safe_create_status_task()

    def _safe_create_status_task(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._publish_status())
        except RuntimeError:
            pass  # No running event loop (e.g. sync unit test)

    async def _publish_status(self) -> None:
        try:
            await self._sink.publish(self.status)
        except Exception as exc:
            self._logger.warning("Failed to publish slm.status: %s", exc)

    def report_success(self, latency_ms: float) -> None:
        """Record a successful inference operation."""
        if self._state == "DISABLED":
            return

        self._consecutive_failures = 0
        self._recent_latencies.append(latency_ms)

        old_state = self._state
        old_breaker = self._breaker

        if self._breaker == "HALF_OPEN":
            self._breaker = "CLOSED"
            self._current_open_s = self._cfg.open_s_initial
            self._reason = "BREAKER_CLOSED_PROBE_SUCCESS"

        # Check for degradation based on p95 of last 20 latencies
        if len(self._recent_latencies) >= 5:
            p95 = statistics.quantiles(self._recent_latencies, n=20)[-1]
            if p95 > self._cfg.degraded_latency_ms:
                self._state = "DEGRADED"
                self._reason = f"LATENCY_DEGRADED_P95_{p95:.1f}MS"
            else:
                self._state = "UP"
                self._reason = "OK"
        else:
            self._state = "UP"
            self._reason = "OK"

        if self._state != old_state or self._breaker != old_breaker:
            self._since_mono_ns = self._clock.now_mono_ns()
            self._safe_create_status_task()

    def report_failure(self, kind: str) -> None:
        """Record an inference or canary failure."""
        if self._state == "DISABLED":
            return

        self._consecutive_failures += 1
        old_state = self._state
        old_breaker = self._breaker

        if self._breaker == "CLOSED":
            if self._consecutive_failures >= self._cfg.failure_threshold:
                self._breaker = "OPEN"
                self._state = "DOWN"
                self._reason = f"BREAKER_OPEN_{kind}"
        elif self._breaker == "HALF_OPEN":
            self._breaker = "OPEN"
            self._state = "DOWN"
            self._current_open_s = min(self._current_open_s * 2.0, self._cfg.open_s_max)
            self._reason = f"CANARY_FAILED_REOPEN_{kind}"

        if self._state != old_state or self._breaker != old_breaker:
            self._since_mono_ns = self._clock.now_mono_ns()
            self._safe_create_status_task()

    async def run_canary(self) -> bool:
        """Run functional canary: 'start the belt' on IDLE must yield label 'start' with p1 >= 0.5."""
        built = self._prompt_builder.build("start the belt", "IDLE")
        timeout_s = self._cfg.canary_timeout_ms / 1000.0

        try:
            tokens = await self._client.score_first_token(built.text, n_probs=10, timeout_s=timeout_s)
            score = self._scorer.score(tokens)

            top_label, top_p = score.top[0] if score.top else ("", 0.0)
            if top_label == "start" and top_p >= 0.5:
                return True
            else:
                self._logger.warning("Canary failed: top_label=%s p1=%.3f", top_label, top_p)
                return False
        except Exception as exc:
            self._logger.warning("Canary threw error: %s", exc)
            return False

    async def start(self) -> None:
        if self._running or self._state == "DISABLED":
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._supervision_loop())

    async def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _supervision_loop(self) -> None:
        """Background health and canary loop."""
        while self._running:
            try:
                if self._breaker == "OPEN":
                    await asyncio.sleep(self._current_open_s)
                    self._breaker = "HALF_OPEN"
                    self._reason = "BREAKER_HALF_OPEN_PROBING"
                    self._since_mono_ns = self._clock.now_mono_ns()
                    await self._publish_status()

                    # Run canary probe
                    success = await self.run_canary()
                    if success:
                        self.report_success(10.0)
                    else:
                        self.report_failure("CANARY_PROBE_FAILED")

                elif self._breaker == "CLOSED":
                    await asyncio.sleep(self._cfg.canary_interval_s)
                    if not self._running:
                        break
                    # Periodic canary check
                    success = await self.run_canary()
                    if not success:
                        self.report_failure("PERIODIC_CANARY_FAILED")
                    else:
                        self.report_success(15.0)

                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._logger.error("Error in supervisor loop: %s", exc)
                await asyncio.sleep(2.0)
