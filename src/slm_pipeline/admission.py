"""admission.py — Inbound utterance admission, rate limiting, single in-flight enforcement, and pipeline execution."""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
import uuid

from slm_pipeline.config import SlmConfig
from slm_pipeline.contracts import (
    Clock,
    EventSink,
    FsmPort,
    LlmClient,
    LlmError,
    Outcome,
    SlmRequest,
    SlmResult,
    Speaker,
    UtteranceUnmatched,
)
from slm_pipeline.prompt import PromptBuilder, sanitize
from slm_pipeline.scorer import LabelScorer
from slm_pipeline.supervisor import SlmSupervisor
from slm_pipeline.tee import SlmTee
from slm_pipeline.validator import OutputValidator

_LOGGER = logging.getLogger(__name__)


class SlmAdmission:
    """Manages admission of unmatched utterances, executes requests, and enforces cancellation."""

    def __init__(
        self,
        cfg: SlmConfig,
        fsm: FsmPort,
        supervisor: SlmSupervisor,
        prompt_builder: PromptBuilder,
        client: LlmClient,
        scorer: LabelScorer,
        validator: OutputValidator,
        tee: SlmTee,
        speaker: Speaker,
        sink: EventSink,
        clock: Clock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cfg = cfg
        self._fsm = fsm
        self._supervisor = supervisor
        self._prompt_builder = prompt_builder
        self._client = client
        self._scorer = scorer
        self._validator = validator
        self._tee = tee
        self._speaker = speaker
        self._sink = sink
        self._clock = clock
        self._logger = logger or _LOGGER

        # Deduplication LRU: last 64 event_ids
        self._recent_event_ids: deque[str] = deque(maxlen=64)

        # Rate limiting: token bucket tracking timestamps of admitted requests
        self._rate_bucket: deque[float] = deque()

        # In-flight task management (Single in flight)
        self._current_task: asyncio.Task[None] | None = None
        self._current_req_id: str | None = None
        self._cancel_reason: str | None = None

        # Unavailable message cooldown
        self._last_unavailable_spoken_ts: float = 0.0

    def on_matched_command(self) -> None:
        """Invoked on matched commands (stt.intent, ui.command) to cancel in-flight SLM inference."""
        if self._current_task and not self._current_task.done():
            self._logger.debug("Cancelling in-flight SLM inference due to matched command")
            self._cancel_reason = "MATCHED_COMMAND"
            self._current_task.cancel()

    async def on_unmatched(self, ev: UtteranceUnmatched) -> None:
        """Handle incoming unmatched utterance."""
        # 1. Feature flag check
        if not self._cfg.enabled:
            return

        # 2. Deduplication check
        if ev.event_id in self._recent_event_ids:
            self._logger.debug("Dropping duplicate event_id: %s", ev.event_id)
            return
        self._recent_event_ids.append(ev.event_id)

        now_ns = self._clock.now_mono_ns()
        req_id = uuid.uuid4().hex

        # 3. Wake word confirmed check
        if not ev.wake_confirmed:
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.NO_COMMAND,
                    reason="NOT_WAKE_CONFIRMED",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            return

        # 4. Input deadline check
        age_ms = (now_ns - ev.ingested_mono_ns) / 1_000_000.0
        if age_ms > self._cfg.max_total_ms:
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.TIMEOUT,
                    reason="STALE_INPUT",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            return

        # 5. Supervisor availability check
        status = self._supervisor.status
        if status.state in ("DOWN", "DISABLED"):
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.UNAVAILABLE,
                    reason=f"SUPERVISOR_{status.state}",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            now_sec = time.time()
            if (now_sec - self._last_unavailable_spoken_ts) >= self._cfg.unavailable_speak_cooldown_s:
                self._last_unavailable_spoken_ts = now_sec
                await self._speaker.say("SLM_UNAVAILABLE")
            return

        # 6. Rate limiter (token bucket per minute)
        now_sec = time.time()
        while self._rate_bucket and (now_sec - self._rate_bucket[0]) > 60.0:
            self._rate_bucket.popleft()

        if len(self._rate_bucket) >= self._cfg.rate_limit_per_min:
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.RATE_LIMITED,
                    reason="RATE_LIMIT_EXCEEDED",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            return
        self._rate_bucket.append(now_sec)

        # 7. Single in flight: cancel existing task (newest wins)
        if self._current_task and not self._current_task.done():
            self._cancel_reason = "NEWEST_WINS"
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass

        # 8 & 9. Launch request task bounded by remaining deadline
        remaining_ms = max(100.0, self._cfg.max_total_ms - age_ms)
        self._cancel_reason = None
        self._current_req_id = req_id

        self._current_task = asyncio.create_task(
            self._execute_request(req_id, ev, remaining_ms)
        )

    async def _execute_request(
        self,
        req_id: str,
        ev: UtteranceUnmatched,
        remaining_ms: float,
    ) -> None:
        """Execute prompt building, inference, scoring, validation, and action dispatch."""
        admitted_mono_ns = self._clock.now_mono_ns()
        admit_latency_ms = (admitted_mono_ns - ev.ingested_mono_ns) / 1_000_000.0

        sanitized_text = sanitize(ev.text)
        if not sanitized_text:
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.NO_COMMAND,
                    reason="EMPTY_AFTER_SANITIZE",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            return

        # Snapshot at prompt creation (FsmPort.snapshot() always returns FsmSnapshot)
        snap_prompt = self._fsm.snapshot()
        slm_req = SlmRequest(
            request_id=req_id,
            event_id=ev.event_id,
            text=sanitized_text,
            ingested_mono_ns=ev.ingested_mono_ns,
            admitted_mono_ns=admitted_mono_ns,
            state_at_prompt=snap_prompt.state,
        )

        # Background acknowledgment timer: speak WORKING if still running after ack_after_ms
        ack_task = asyncio.create_task(self._ack_timer(self._cfg.ack_after_ms / 1000.0))

        try:
            async with asyncio.timeout(remaining_ms / 1000.0):
                # 1. Build prompt
                built_prompt = self._prompt_builder.build(sanitized_text, snap_prompt.state)

                # 2. Call LLM
                prefill_start_ns = self._clock.now_mono_ns()
                tokens = await self._client.score_first_token(
                    built_prompt.text,
                    n_probs=self._cfg.llama.n_probs,
                    timeout_s=self._cfg.llama.request_timeout_ms / 1000.0,
                )
                prefill_end_ns = self._clock.now_mono_ns()
                prefill_gen_ms = (prefill_end_ns - prefill_start_ns) / 1_000_000.0

                # 3. Label score
                score = self._scorer.score(tokens)

                # 4. Fresh FSM snapshot at decision time (FsmPort.snapshot() always returns FsmSnapshot)
                val_start_ns = self._clock.now_mono_ns()
                snap_decision = self._fsm.snapshot()
                total_age_ms = (val_start_ns - ev.ingested_mono_ns) / 1_000_000.0

                # 5. Output validator
                vr = self._validator.validate(score, sanitized_text, snap_decision, total_age_ms)
                val_end_ns = self._clock.now_mono_ns()
                validate_ms = (val_end_ns - val_start_ns) / 1_000_000.0
                total_ms = (val_end_ns - ev.ingested_mono_ns) / 1_000_000.0

                action_request_id: str | None = None

                # 6. Dispatch action or spoken feedback
                if vr.outcome == Outcome.ACTION:
                    action_request_id = await self._tee.run(
                        vr, slm_req, score, snap_decision, self._prompt_builder.version
                    )
                elif vr.speak_key:
                    await self._speaker.say(vr.speak_key, **vr.speak_params)

                # 7. Publish slm.result (NO UTTERANCE TEXT!)
                result = SlmResult(
                    request_id=req_id,
                    outcome=vr.outcome,
                    label=vr.label,
                    intent_class=vr.intent_class,
                    p1=score.p1,
                    margin=score.margin,
                    reason=vr.reason,
                    state_at_prompt=snap_prompt.state,
                    state_at_decision=snap_decision.state,
                    latency_ms={
                        "admit": admit_latency_ms,
                        "prefill_gen": prefill_gen_ms,
                        "validate": validate_ms,
                        "total": total_ms,
                    },
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                    action_request_id=action_request_id,
                )
                await self._sink.publish(result)
                self._supervisor.report_success(total_ms)

        except asyncio.CancelledError:
            reason = self._cancel_reason or "CANCELLED"
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.CANCELLED,
                    reason=reason,
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            raise
        except asyncio.TimeoutError:
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.TIMEOUT,
                    reason="TOTAL_DEADLINE_EXCEEDED",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
            self._supervisor.report_failure("TIMEOUT")
        except LlmError as exc:
            self._supervisor.report_failure(exc.kind)
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.ERROR,
                    reason=f"LLM_{exc.kind}",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
        except Exception as exc:
            self._logger.error("Unexpected error in SLM execution: %s", exc)
            self._supervisor.report_failure("EXCEPTION")
            await self._sink.publish(
                SlmResult(
                    request_id=req_id,
                    outcome=Outcome.ERROR,
                    reason="INTERNAL_ERROR",
                    model_id="qwen3",
                    prompt_version=self._prompt_builder.version,
                )
            )
        finally:
            ack_task.cancel()
            try:
                await ack_task
            except asyncio.CancelledError:
                pass

    async def _ack_timer(self, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
            await self._speaker.say("WORKING")
        except asyncio.CancelledError:
            pass
