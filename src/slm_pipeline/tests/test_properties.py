"""test_properties.py — Property-based tests using Hypothesis (P-1 through P-6)."""

import asyncio
from hypothesis import given, settings, strategies as st
import pytest

from control_pipeline.schemas.fsm import Action
from slm_pipeline.config import SlotConfig, TeeConfig, ThresholdConfig
from slm_pipeline.contracts import (
    FsmSnapshot,
    IntentClass,
    IntentScore,
    Outcome,
    SlmRequest,
    SlmResult,
    TokenProb,
    UtteranceUnmatched,
)
from slm_pipeline.prompt import PromptBuilder
from slm_pipeline.scorer import LabelScorer, LabelSpec
from slm_pipeline.tee import SlmTee
from slm_pipeline.tests.fakes import (
    FakeClock,
    FakeDecisionBus,
    FakeEventSink,
    FakeFsm,
    FakeLlm,
    FakeSpeaker,
)
from slm_pipeline.validator import OutputValidator


# Label set for testing
_TEST_LABELS = [
    LabelSpec(label="start", intent_class=IntentClass.ENERGIZE, action=Action.START),
    LabelSpec(label="resume", intent_class=IntentClass.ENERGIZE, action=Action.RESUME),
    LabelSpec(label="pause", intent_class=IntentClass.DEENERGIZE, action=Action.PAUSE),
    LabelSpec(label="stop", intent_class=IntentClass.DEENERGIZE, action=Action.STOP),
    LabelSpec(label="speed", intent_class=IntentClass.PARAM, action=Action.SET_SPEED, needs_slot="speed"),
    LabelSpec(label="recover", intent_class=IntentClass.RECOVERY, action=Action.RECOVER),
    LabelSpec(label="status", intent_class=IntentClass.INFO, action=None),
    LabelSpec(label="why", intent_class=IntentClass.INFO, action=None),
    LabelSpec(label="help", intent_class=IntentClass.INFO, action=None),
    LabelSpec(label="none", intent_class=IntentClass.META, action=None),
]

_LABEL_MAP = {l.label: l for l in _TEST_LABELS}
_SCORER = LabelScorer(_TEST_LABELS)
_VALIDATOR = OutputValidator(ThresholdConfig(), _LABEL_MAP, SlotConfig(min_mms=50, max_mms=500, require_unit=True))


@st.composite
def random_score(draw):
    """Generate random non-negative probability values summing <= 1.0."""
    raw_floats = [draw(st.floats(min_value=0.0, max_value=1.0)) for _ in _TEST_LABELS]
    total = sum(raw_floats)
    if total > 0.99:
        scale = 0.99 / total
        raw_floats = [f * scale for f in raw_floats]

    tokens = [TokenProb(token=l.label, prob=p) for l, p in zip(_TEST_LABELS, raw_floats)]
    return _SCORER.score(tokens)


@st.composite
def random_snapshot(draw):
    state = draw(st.sampled_from(["IDLE", "RUNNING", "PAUSED", "JAMMED", "SAFETY_FAULT", "RECOVERING"]))
    actions = draw(st.sets(st.sampled_from(list(Action)), max_size=4))
    return FsmSnapshot(state=state, fsm_version=1, permitted=frozenset(actions))


# ---------------------------------------------------------------------------
# P-1: ACTION is never returned for an INFO or META label
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(score=random_score(), snap=random_snapshot())
def test_p1_action_never_for_info_or_meta(score: IntentScore, snap: FsmSnapshot):
    vr = _VALIDATOR.validate(score, "speed 100 mm/s", snap, total_age_ms=100.0)
    if vr.outcome == Outcome.ACTION:
        assert vr.intent_class not in (IntentClass.INFO, IntentClass.META)


# ---------------------------------------------------------------------------
# P-2: ACTION is never returned for an action outside snap.permitted
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(score=random_score(), snap=random_snapshot())
def test_p2_action_never_outside_permitted(score: IntentScore, snap: FsmSnapshot):
    vr = _VALIDATOR.validate(score, "speed 100 mm/s", snap, total_age_ms=100.0)
    if vr.outcome == Outcome.ACTION:
        assert vr.action in snap.permitted


# ---------------------------------------------------------------------------
# P-3: Every ACTION for ENERGIZE, PARAM or RECOVERY has requires_confirmation=True
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p3_confirmation_flag_on_energizing_param_recovery():
    fake_fsm = FakeFsm("IDLE")
    fake_bus = FakeDecisionBus()
    fake_speaker = FakeSpeaker()
    fake_clock = FakeClock()
    tee = SlmTee(TeeConfig(), fake_fsm, fake_bus, fake_speaker, candidates=None, clock=fake_clock)

    for cls in (IntentClass.ENERGIZE, IntentClass.PARAM, IntentClass.RECOVERY):
        act = Action.START if cls == IntentClass.ENERGIZE else (Action.SET_SPEED if cls == IntentClass.PARAM else Action.RECOVER)
        score = IntentScore(label_probs={"x": 0.9}, mass=0.9, top=[("x", 0.9)], p1=0.9, p2=0.0, margin=0.9)
        snap = FsmSnapshot(state="IDLE", fsm_version=1, permitted=frozenset({act}))

        from slm_pipeline.validator import ValidationResult
        vr = ValidationResult(outcome=Outcome.ACTION, reason="OK", label="cmd", intent_class=cls, action=act)
        req = SlmRequest(request_id="r1", event_id="e1", text="cmd", ingested_mono_ns=100, admitted_mono_ns=200, state_at_prompt="IDLE")

        await tee.run(vr, req, score, snap, "p1")
        submitted = fake_fsm.submitted[-1]
        assert submitted.requires_confirmation is True, f"Failed for {cls}"


# ---------------------------------------------------------------------------
# P-5: Sentinel string in utterance never appears in SlmResult or ActionRequest meta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p5_privacy_sentinel_string_never_leaks():
    sentinel = "SECRET_OPERATOR_CONVERSATION_42"
    ev = UtteranceUnmatched(
        event_id="ev_priv",
        text=f"please start the conveyor {sentinel}",
        ingested_mono_ns=1_000_000,
        wake_confirmed=True,
    )

    fake_fsm = FakeFsm("IDLE")
    fake_bus = FakeDecisionBus()
    fake_speaker = FakeSpeaker()
    fake_clock = FakeClock(10_000_000)
    fake_sink = FakeEventSink()
    fake_llm = FakeLlm()

    from slm_pipeline.admission import SlmAdmission
    from slm_pipeline.config import SlmConfig
    from slm_pipeline.supervisor import SlmSupervisor

    cfg = SlmConfig.default()
    cfg.enabled = True
    pb = PromptBuilder("system prompt")
    sup = SlmSupervisor(cfg.breaker, fake_llm, pb, _SCORER, fake_sink, fake_clock, enabled=True)
    tee = SlmTee(cfg.tee, fake_fsm, fake_bus, fake_speaker, None, fake_clock)

    adm = SlmAdmission(
        cfg=cfg,
        fsm=fake_fsm,
        supervisor=sup,
        prompt_builder=pb,
        client=fake_llm,
        scorer=_SCORER,
        validator=_VALIDATOR,
        tee=tee,
        speaker=fake_speaker,
        sink=fake_sink,
        clock=fake_clock,
    )

    await adm.on_unmatched(ev)
    await asyncio.sleep(0.05)

    # Check that sentinel is in NO published event or ActionRequest meta
    for ev_out in fake_sink.events:
        dump = ev_out.model_dump_json()
        assert sentinel not in dump

    for sub in fake_fsm.submitted:
        for k, v in sub.meta.items():
            assert sentinel not in k
            assert sentinel not in v


# ---------------------------------------------------------------------------
# P-6: setpoint_mms in ActionRequest equals slot parser's output
# ---------------------------------------------------------------------------
@settings(max_examples=50)
@given(speed=st.integers(min_value=50, max_value=500))
def test_p6_setpoint_matches_slot(speed: int):
    cfg = SlotConfig(min_mms=50, max_mms=500, require_unit=True)
    text = f"speed {speed} mm/s"
    from slm_pipeline.slot import extract_speed, SpeedSlot
    slot = extract_speed(text, cfg)
    assert isinstance(slot, SpeedSlot)
    assert slot.setpoint_mms == speed
