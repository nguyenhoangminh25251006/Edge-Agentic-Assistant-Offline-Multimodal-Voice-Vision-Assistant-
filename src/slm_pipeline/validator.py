"""validator.py — OutputValidator evaluating rules V1–V10 against score, slot, and FSM snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from control_pipeline.schemas.fsm import Action
from slm_pipeline.config import SlotConfig, ThresholdConfig
from slm_pipeline.contracts import FsmSnapshot, IntentClass, IntentScore, Outcome
from slm_pipeline.scorer import LabelSpec
from slm_pipeline.slot import SlotError, SpeedSlot, extract_speed


@dataclass(frozen=True)
class ValidationResult:
    outcome: Outcome
    reason: str
    label: str | None = None
    intent_class: IntentClass | None = None
    action: Action | None = None
    params: dict[str, int] = field(default_factory=dict)
    speak_key: str | None = None
    speak_params: dict[str, object] = field(default_factory=dict)


class OutputValidator:
    """Evaluates rules V1 to V10 strictly without performing any I/O."""

    def __init__(
        self,
        cfg: ThresholdConfig,
        labels: dict[str, LabelSpec],
        speed_cfg: SlotConfig,
        max_total_ms: float = 6000.0,
    ) -> None:
        self._cfg = cfg
        self._labels = labels
        self._speed_cfg = speed_cfg
        self._max_total_ms = max_total_ms

    def validate(
        self,
        score: IntentScore,
        text: str,
        snap: FsmSnapshot,
        total_age_ms: float,
    ) -> ValidationResult:
        """Evaluate rules V1 to V10 in sequence; first match returns."""
        # V1: Total deadline expiry
        if total_age_ms > self._max_total_ms:
            return ValidationResult(outcome=Outcome.TIMEOUT, reason="STALE_RESULT")

        # V2: Mass below minimum label mass threshold
        if score.mass < self._cfg.min_label_mass:
            return ValidationResult(
                outcome=Outcome.CLARIFY,
                reason="LOW_LABEL_MASS",
                speak_key="CLARIFY_NONE",
            )

        top_label, top_p = score.top[0]
        label_spec = self._labels.get(top_label)

        # V3 & V4: Top label is 'none'
        if top_label == "none":
            if score.p2 < self._cfg.none_clarify_p2:
                # V3: Silent drop
                return ValidationResult(
                    outcome=Outcome.NO_COMMAND,
                    reason="NONE_LABEL",
                    label=top_label,
                    intent_class=IntentClass.META,
                )
            else:
                # V4: Ambiguous none; clarify with top 2 command candidates
                command_candidates = [lbl for lbl, _ in score.top if lbl != "none"]
                a = command_candidates[0] if len(command_candidates) > 0 else "action"
                b = command_candidates[1] if len(command_candidates) > 1 else "none"
                return ValidationResult(
                    outcome=Outcome.CLARIFY,
                    reason="NONE_AMBIGUOUS",
                    label=top_label,
                    intent_class=IntentClass.META,
                    speak_key="CLARIFY_TWO",
                    speak_params={"a": a, "b": b},
                )

        if label_spec is None:
            return ValidationResult(
                outcome=Outcome.CLARIFY,
                reason="LOW_LABEL_MASS",
                speak_key="CLARIFY_NONE",
            )

        # V5: Threshold group for top label
        if label_spec.intent_class == IntentClass.DEENERGIZE:
            grp = self._cfg.deenergize
        elif label_spec.intent_class in (IntentClass.ENERGIZE, IntentClass.PARAM, IntentClass.RECOVERY):
            grp = self._cfg.energize
        else:
            grp = self._cfg.info

        if score.p1 < grp.min_p or score.margin < grp.min_margin:
            reason = "LOW_P" if score.p1 < grp.min_p else "LOW_MARGIN"
            if score.p2 >= self._cfg.clarify_p2 and len(score.top) >= 2:
                return ValidationResult(
                    outcome=Outcome.CLARIFY,
                    reason=reason,
                    label=top_label,
                    intent_class=label_spec.intent_class,
                    speak_key="CLARIFY_TWO",
                    speak_params={"a": score.top[0][0], "b": score.top[1][0]},
                )
            else:
                return ValidationResult(
                    outcome=Outcome.CLARIFY,
                    reason=reason,
                    label=top_label,
                    intent_class=label_spec.intent_class,
                    speak_key="CLARIFY_NONE",
                )

        # V6: Class INFO (deterministic spoken answers)
        if label_spec.intent_class == IntentClass.INFO:
            if top_label == "status":
                return ValidationResult(
                    outcome=Outcome.ANSWER,
                    reason="INFO_STATUS",
                    label=top_label,
                    intent_class=label_spec.intent_class,
                    speak_key=f"STATUS_{snap.state.upper()}",
                )
            elif top_label == "why":
                if snap.last_reason:
                    return ValidationResult(
                        outcome=Outcome.ANSWER,
                        reason="INFO_WHY",
                        label=top_label,
                        intent_class=label_spec.intent_class,
                        speak_key=f"REASON_{snap.last_reason}",
                    )
                else:
                    return ValidationResult(
                        outcome=Outcome.ANSWER,
                        reason="INFO_WHY",
                        label=top_label,
                        intent_class=label_spec.intent_class,
                        speak_key="WHY_NONE",
                    )
            elif top_label == "help":
                return ValidationResult(
                    outcome=Outcome.ANSWER,
                    reason="INFO_HELP",
                    label=top_label,
                    intent_class=label_spec.intent_class,
                    speak_key="HELP_CANNED",
                )

        # V7: Label needs slot (speed)
        params: dict[str, int] = {}
        if label_spec.needs_slot == "speed":
            slot = extract_speed(text, self._speed_cfg)
            if isinstance(slot, SlotError):
                if slot == SlotError.NEED_UNIT:
                    sp_key = "NEED_UNIT"
                elif slot == SlotError.AMBIGUOUS:
                    sp_key = "SPEED_AMBIGUOUS"
                else:
                    sp_key = "NEED_SPEED"
                return ValidationResult(
                    outcome=Outcome.CLARIFY,
                    reason=slot.value,
                    label=top_label,
                    intent_class=label_spec.intent_class,
                    speak_key=sp_key,
                )

            # V8: Speed limits check
            if (self._speed_cfg.min_mms is not None and slot.setpoint_mms < self._speed_cfg.min_mms) or (
                self._speed_cfg.max_mms is not None and slot.setpoint_mms > self._speed_cfg.max_mms
            ):
                return ValidationResult(
                    outcome=Outcome.REJECTED_RANGE,
                    reason="SPEED_RANGE",
                    label=top_label,
                    intent_class=label_spec.intent_class,
                    speak_key="SPEED_RANGE",
                    speak_params={
                        "min": self._speed_cfg.min_mms or 0,
                        "max": self._speed_cfg.max_mms or 9999,
                    },
                )
            params["setpoint_mms"] = slot.setpoint_mms

        # V9: Action permitted in current FSM snapshot
        action = label_spec.action
        if action is not None and action not in snap.permitted:
            return ValidationResult(
                outcome=Outcome.ILLEGAL_STATE,
                reason="NOT_PERMITTED",
                label=top_label,
                intent_class=label_spec.intent_class,
                action=action,
                speak_key="ILLEGAL_ACTION",
                speak_params={"action": action.value, "state": snap.state},
            )

        # V10: Nominal ACTION
        return ValidationResult(
            outcome=Outcome.ACTION,
            reason="OK",
            label=top_label,
            intent_class=label_spec.intent_class,
            action=action,
            params=params,
        )
