"""test_validator.py — Tests for rules V1–V10 of OutputValidator (U-6)."""

import pytest
from control_pipeline.schemas.fsm import Action
from slm_pipeline.config import SlotConfig, ThresholdConfig
from slm_pipeline.contracts import FsmSnapshot, IntentClass, IntentScore, Outcome
from slm_pipeline.scorer import LabelSpec
from slm_pipeline.validator import OutputValidator


@pytest.fixture
def validator_setup():
    cfg = ThresholdConfig()
    labels = {
        "start": LabelSpec(label="start", intent_class=IntentClass.ENERGIZE, action=Action.START),
        "stop": LabelSpec(label="stop", intent_class=IntentClass.DEENERGIZE, action=Action.STOP),
        "pause": LabelSpec(label="pause", intent_class=IntentClass.DEENERGIZE, action=Action.PAUSE),
        "speed": LabelSpec(label="speed", intent_class=IntentClass.PARAM, action=Action.SET_SPEED, needs_slot="speed"),
        "status": LabelSpec(label="status", intent_class=IntentClass.INFO, action=None),
        "why": LabelSpec(label="why", intent_class=IntentClass.INFO, action=None),
        "help": LabelSpec(label="help", intent_class=IntentClass.INFO, action=None),
        "none": LabelSpec(label="none", intent_class=IntentClass.META, action=None),
    }
    speed_cfg = SlotConfig(min_mms=50, max_mms=500, require_unit=True)
    validator = OutputValidator(cfg, labels, speed_cfg, max_total_ms=6000.0)
    return validator


def make_score(top_label: str, p1: float, p2: float = 0.05, mass: float = 0.90) -> IntentScore:
    return IntentScore(
        label_probs={top_label: p1},
        mass=mass,
        top=[(top_label, p1), ("other", p2)],
        p1=p1,
        p2=p2,
        margin=p1 - p2,
    )


def test_v1_stale_result(validator_setup):
    snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({Action.START}))
    score = make_score("start", 0.90)
    res = validator_setup.validate(score, "start belt", snap, total_age_ms=6500.0)
    assert res.outcome == Outcome.TIMEOUT
    assert res.reason == "STALE_RESULT"


def test_v2_low_label_mass(validator_setup):
    snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({Action.START}))
    score = make_score("start", 0.30, mass=0.40)  # min_label_mass is 0.50
    res = validator_setup.validate(score, "start belt", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.CLARIFY
    assert res.reason == "LOW_LABEL_MASS"
    assert res.speak_key == "CLARIFY_NONE"


def test_v3_none_silent(validator_setup):
    snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({Action.START}))
    score = make_score("none", 0.80, p2=0.10)  # p2 < none_clarify_p2 (0.30)
    res = validator_setup.validate(score, "nice weather", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.NO_COMMAND
    assert res.reason == "NONE_LABEL"


def test_v4_none_ambiguous(validator_setup):
    snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({Action.START}))
    score = make_score("none", 0.55, p2=0.35)  # p2 >= 0.30
    res = validator_setup.validate(score, "maybe start", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.CLARIFY
    assert res.reason == "NONE_AMBIGUOUS"
    assert res.speak_key == "CLARIFY_TWO"


def test_v5_low_margin_or_p(validator_setup):
    snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({Action.START}))
    # energize requires min_p=0.80 and min_margin=0.50
    score = make_score("start", 0.70, p2=0.25)
    res = validator_setup.validate(score, "start", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.CLARIFY
    assert res.reason in ("LOW_P", "LOW_MARGIN")


def test_v6_info_answers(validator_setup):
    snap = FsmSnapshot(state="RUNNING", fsm_version=1, permitted=frozenset(), last_reason="ERR_ESTOP_ACTIVE")

    # status
    score_status = make_score("status", 0.90)
    res = validator_setup.validate(score_status, "what is status", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.ANSWER
    assert res.speak_key == "STATUS_RUNNING"

    # why
    score_why = make_score("why", 0.90)
    res = validator_setup.validate(score_why, "why stopped", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.ANSWER
    assert res.speak_key == "REASON_ERR_ESTOP_ACTIVE"


def test_v7_v8_speed_slot(validator_setup):
    snap = FsmSnapshot(state="RUNNING", fsm_version=1, permitted=frozenset({Action.SET_SPEED}))

    # V7: Need unit
    score = make_score("speed", 0.90)
    res = validator_setup.validate(score, "set speed 200", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.CLARIFY
    assert res.speak_key == "NEED_UNIT"

    # V8: Out of speed range [50, 500]
    res_range = validator_setup.validate(score, "set speed 600 mm/s", snap, total_age_ms=100.0)
    assert res_range.outcome == Outcome.REJECTED_RANGE
    assert res_range.speak_key == "SPEED_RANGE"


def test_v9_illegal_state(validator_setup):
    # FSM is RUNNING: Action.START is not in permitted
    snap = FsmSnapshot(state="RUNNING", fsm_version=1, permitted=frozenset({Action.STOP, Action.PAUSE}))
    score = make_score("start", 0.95, p2=0.01)
    res = validator_setup.validate(score, "start the belt", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.ILLEGAL_STATE
    assert res.reason == "NOT_PERMITTED"
    assert res.speak_key == "ILLEGAL_ACTION"


def test_v10_nominal_action(validator_setup):
    snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({Action.START}))
    score = make_score("start", 0.95, p2=0.02)
    res = validator_setup.validate(score, "please start", snap, total_age_ms=100.0)
    assert res.outcome == Outcome.ACTION
    assert res.action == Action.START
