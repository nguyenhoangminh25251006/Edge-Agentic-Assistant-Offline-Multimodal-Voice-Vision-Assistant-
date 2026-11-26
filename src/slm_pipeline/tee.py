"""tee.py — ToolExecutionEngine submitting ActionRequest and processing DecisionBus events."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from slm_pipeline.config import TeeConfig
from slm_pipeline.contracts import (
    ActionRequest,
    Clock,
    DecisionBus,
    DecisionEvent,
    FsmPort,
    FsmSnapshot,
    IntentClass,
    IntentScore,
    SlmRequest,
    Speaker,
)
from slm_pipeline.validator import ValidationResult

_LOGGER = logging.getLogger(__name__)


class SlmTee:
    """Builds and submits ActionRequest from ACTION ValidationResult and monitors decision bus."""

    def __init__(
        self,
        cfg: TeeConfig,
        fsm: FsmPort,
        bus: DecisionBus,
        speaker: Speaker,
        candidates: Any | None,
        clock: Clock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._cfg = cfg
        self._fsm = fsm
        self._bus = bus
        self._speaker = speaker
        self._candidates = candidates
        self._clock = clock
        self._logger = logger or _LOGGER
        # Keep strong references to follow tasks to prevent GC cancellation
        self._pending_tasks: set[asyncio.Task[None]] = set()

    async def run(
        self,
        vr: ValidationResult,
        req: SlmRequest,
        score: IntentScore,
        snap: FsmSnapshot,
        prompt_version: str,
    ) -> str:
        """Construct ActionRequest, submit, and listen for feedback on DecisionBus."""
        if vr.action is None:
            raise ValueError("ValidationResult must carry an action for Tee execution")

        action_req_id = uuid.uuid4().hex
        now_ns = self._clock.now_mono_ns()

        # AR-SLM-2 / AR-SLM-3: Energizing, Param, Recovery require confirmation; De-energize does not
        requires_confirmation = vr.intent_class in (
            IntentClass.ENERGIZE,
            IntentClass.PARAM,
            IntentClass.RECOVERY,
        )

        action_req = ActionRequest(
            request_id=action_req_id,
            origin="SLM",
            action=vr.action,
            params=vr.params,
            created_mono_ns=now_ns,
            expires_in_ms=self._cfg.request_ttl_ms,
            context_fsm_version=snap.fsm_version,
            confidence=score.p1,
            requires_confirmation=requires_confirmation,
            meta={
                "slm_request_id": req.request_id,
                "prompt_version": prompt_version,
            },
        )

        # Register decision-bus subscription BEFORE submitting so no NEED_CONFIRMATION is missed.
        # asyncio.create_task schedules the coroutine but it won't run until the next await.
        # We yield with asyncio.sleep(0) to let the task reach its first bus.subscribe() call,
        # registering the queue before submit() synchronously publishes the decision.
        follow_task = asyncio.create_task(self._follow_decision(action_req_id, vr, req.text))
        await asyncio.sleep(0)  # yield so follow_task registers its queue in the bus

        # Submit to FSM/Arbiter (may publish NEED_CONFIRMATION synchronously)
        await self._fsm.submit(action_req)
        _ = follow_task  # kept alive; it will run to completion on its own

        return action_req_id

    async def _follow_decision(
        self,
        action_req_id: str,
        vr: ValidationResult,
        utterance_text: str,
    ) -> None:
        """Listen to DecisionBus for verdict updates until terminal event or timeout."""
        follow_timeout_s = self._cfg.follow_ms / 1000.0

        try:
            async with asyncio.timeout(follow_timeout_s):
                async for event in self._bus.subscribe(action_req_id):
                    if event.request_id != action_req_id:
                        continue

                    if event.verdict == "NEED_CONFIRMATION":
                        # Read-back speech
                        action_name = vr.action.value.upper() if vr.action else "ACTION"
                        sp_key = f"READBACK_{action_name}"
                        sp_params: dict[str, object] = {}
                        if "setpoint_mms" in vr.params:
                            sp_params["speed"] = vr.params["setpoint_mms"]

                        await self._speaker.say(sp_key, **sp_params)

                    elif event.verdict == "REJECT":
                        sp_key = f"REASON_{event.reason_code}" if event.reason_code else "REASON_ERR_UNKNOWN_CMD"
                        await self._speaker.say(sp_key)
                        break

                    elif event.verdict == "EXPIRED":
                        await self._speaker.say("CONFIRM_EXPIRED")
                        break

                    elif event.verdict in ("CONFIRMED", "ACCEPT"):
                        if event.mcu_result == "ACCEPTED" and self._candidates is not None:
                            try:
                                if vr.label:
                                    await self._candidates.record(utterance_text, vr.label)
                            except Exception as exc:
                                self._logger.warning("Failed to record candidate pattern: %s", exc)
                        break

                    elif event.verdict == "CANCELLED":
                        break
        except asyncio.TimeoutError:
            self._logger.debug("Decision bus follow timed out for %s", action_req_id)
        except Exception as exc:
            self._logger.error("Error in decision bus listener for %s: %s", action_req_id, exc)
