"""service.py — SLM Pipeline lifecycle service wiring components, startup checks, and event dispatch."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import yaml

from control_pipeline.schemas.fsm import Action
from slm_pipeline.admission import SlmAdmission
from slm_pipeline.client import LlamaServerClient
from slm_pipeline.config import SlmConfig
from slm_pipeline.contracts import (
    Clock,
    DecisionBus,
    EventSink,
    FsmPort,
    IntentClass,
    LlmClient,
    Speaker,
    UtteranceUnmatched,
)
from slm_pipeline.prompt import PromptBuilder
from slm_pipeline.scorer import LabelScorer, LabelSpec
from slm_pipeline.supervisor import SlmSupervisor
from slm_pipeline.tee import SlmTee
from slm_pipeline.validator import OutputValidator

_LOGGER = logging.getLogger(__name__)


class DefaultClock:
    def now_mono_ns(self) -> int:
        import time
        return time.monotonic_ns()


class SlmService:
    """Master service managing the SLM pipeline lifecycle, resource verification, and event registration."""

    def __init__(
        self,
        cfg: SlmConfig | None = None,
        fsm: FsmPort | None = None,
        bus: DecisionBus | None = None,
        speaker: Speaker | None = None,
        sink: EventSink | None = None,
        clock: Clock | None = None,
        client: LlmClient | None = None,
        resource_dir: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cfg = cfg or SlmConfig.default()
        self._fsm = fsm
        self._bus = bus
        self._speaker = speaker
        self._sink = sink
        self._clock = clock or DefaultClock()
        self._custom_client = client
        self._resource_dir = resource_dir or (Path(__file__).parent / "resources")
        self._logger = logger or _LOGGER

        self._labels: list[LabelSpec] = []
        self._phrases: dict[str, str] = {}
        self._prompt_static: str = ""

        self._prompt_builder: PromptBuilder | None = None
        self._scorer: LabelScorer | None = None
        self._validator: OutputValidator | None = None
        self._client: LlmClient | None = None
        self._tee: SlmTee | None = None
        self._supervisor: SlmSupervisor | None = None
        self._admission: SlmAdmission | None = None

        self._started = False

    @property
    def admission(self) -> SlmAdmission | None:
        return self._admission

    @property
    def supervisor(self) -> SlmSupervisor | None:
        return self._supervisor

    async def initialize(self) -> bool:
        """Run startup sequence and validations (§4.12)."""
        if not self._cfg.enabled:
            self._logger.info("SLM pipeline is disabled by configuration (enabled=False)")
            return True

        # 1. Load resources
        try:
            self._load_resources()
        except Exception as exc:
            self._logger.error("Failed to load SLM resources: %s", exc)
            return False

        # 2. Check labels & phrases consistency
        self._scorer = LabelScorer(self._labels)
        if "none" not in self._scorer.label_map:
            self._logger.error("Startup check failed: label 'none' missing in labels.yaml")
            return False

        # Required phrase keys check
        required_phrase_keys = [
            "SLM_UNAVAILABLE", "WORKING", "CLARIFY_NONE", "CLARIFY_TWO",
            "NEED_SPEED", "NEED_UNIT", "SPEED_AMBIGUOUS", "SPEED_RANGE",
            "ILLEGAL_ACTION", "READBACK_START", "READBACK_RESUME",
            "READBACK_SET_SPEED", "READBACK_RECOVER", "CONFIRM_EXPIRED",
            "STATUS_IDLE", "STATUS_RUNNING", "STATUS_PAUSED", "STATUS_JAMMED",
            "STATUS_SAFETY_FAULT", "STATUS_RECOVERING", "WHY_NONE", "HELP_CANNED",
        ]
        missing_phrases = [k for k in required_phrase_keys if k not in self._phrases]
        if missing_phrases:
            self._logger.error("Startup check failed: missing phrase keys: %s", missing_phrases)
            return False

        self._prompt_builder = PromptBuilder(self._prompt_static)
        self._validator = OutputValidator(
            self._cfg.thresholds,
            self._scorer.label_map,
            self._cfg.speed,
            self._cfg.max_total_ms,
        )

        # 3. Create or use client
        if self._custom_client is not None:
            self._client = self._custom_client
        else:
            self._client = LlamaServerClient(
                base_url=self._cfg.llama.base_url,
                connect_timeout_s=self._cfg.llama.connect_timeout_ms / 1000.0,
                request_timeout_s=self._cfg.llama.request_timeout_ms / 1000.0,
            )

        # 4. Verify labels tokenization
        ok, token_failures = await self._scorer.verify_labels(self._client)
        if not ok:
            self._logger.error("Startup label verification failed: %s", token_failures)
            # Pipeline is marked disabled
            return False

        # 5. Build supervisor & admission
        if self._sink and self._fsm and self._bus and self._speaker:
            self._tee = SlmTee(
                self._cfg.tee,
                self._fsm,
                self._bus,
                self._speaker,
                candidates=None,
                clock=self._clock,
                logger=self._logger.getChild("tee"),
            )
            self._supervisor = SlmSupervisor(
                self._cfg.breaker,
                self._client,
                self._prompt_builder,
                self._scorer,
                self._sink,
                self._clock,
                enabled=self._cfg.enabled,
                logger=self._logger.getChild("supervisor"),
            )
            self._admission = SlmAdmission(
                self._cfg,
                self._fsm,
                self._supervisor,
                self._prompt_builder,
                self._client,
                self._scorer,
                self._validator,
                self._tee,
                self._speaker,
                self._sink,
                self._clock,
                logger=self._logger.getChild("admission"),
            )

            # 6. Initial Canary probe
            canary_ok = await self._supervisor.run_canary()
            if not canary_ok:
                self._logger.warning("Initial canary probe failed; starting in DOWN state")
                self._supervisor.report_failure("INITIAL_CANARY_FAILED")

            # 7. Start supervisor loop
            await self._supervisor.start()

        self._started = True
        return True

    def _load_resources(self) -> None:
        """Load YAML and text resource files."""
        labels_path = self._resource_dir / "labels.yaml"
        with open(labels_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            self._labels = []
            for item in data.get("labels", []):
                act = Action(item["action"].lower()) if item.get("action") else None
                cls_enum = IntentClass(item["class"])
                self._labels.append(
                    LabelSpec(
                        label=item["label"],
                        intent_class=cls_enum,
                        action=act,
                        needs_slot=item.get("needs_slot"),
                    )
                )

        phrases_path = self._resource_dir / "phrases.yaml"
        with open(phrases_path, "r", encoding="utf-8") as f:
            self._phrases = yaml.safe_load(f) or {}

        prompt_path = self._resource_dir / "prompt_static.txt"
        with open(prompt_path, "r", encoding="utf-8") as f:
            self._prompt_static = f.read()

    async def on_unmatched(self, ev: UtteranceUnmatched) -> None:
        if self._admission:
            await self._admission.on_unmatched(ev)

    def on_matched_command(self) -> None:
        if self._admission:
            self._admission.on_matched_command()

    async def stop(self) -> None:
        if self._supervisor:
            await self._supervisor.stop()
        if isinstance(self._client, LlamaServerClient):
            await self._client.aclose()
        self._started = False
