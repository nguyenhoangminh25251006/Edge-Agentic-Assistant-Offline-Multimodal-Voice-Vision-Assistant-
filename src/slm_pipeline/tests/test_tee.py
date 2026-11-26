"""test_tee.py — Tests for ToolExecutionEngine ActionRequest generation and bus handling (U-7)."""

import asyncio
import pytest

from control_pipeline.schemas.fsm import Action
from slm_pipeline.config import TeeConfig
from slm_pipeline.contracts import (
    DecisionEvent,
    FsmSnapshot,
    IntentClass,
    IntentScore,
    Outcome,
    SlmRequest,
)
from slm_pipeline.tee import SlmTee
from slm_pipeline.tests.fakes import (
    FakeClock,
    FakeDecisionBus,
    FakeFsm,
    FakeSpeaker,
)
from slm_pipeline.validator import ValidationResult


@pytest.mark.asyncio
async def test_tee_energize_submits_requires_confirmation_and_reads_back():
    cfg = TeeConfig(request_ttl_ms=3000, follow_ms=2000)
    fake_fsm = FakeFsm("IDLE")
    fake_bus = FakeDecisionBus()
    fake_speaker = FakeSpeaker()
    fake_clock = FakeClock(1_000_000_000)

    tee = SlmTee(cfg, fake_fsm, fake_bus, fake_speaker, candidates=None, clock=fake_clock)

    vr = ValidationResult(
        outcome=Outcome.ACTION,
        reason="OK",
        label="start",
        intent_class=IntentClass.ENERGIZE,
        action=Action.START,
    )
    req = SlmRequest(
        request_id="req123",
        event_id="ev123",
        text="start the belt",
        ingested_mono_ns=900_000_000,
        admitted_mono_ns=950_000_000,
        state_at_prompt="IDLE",
    )
    score = IntentScore(
        label_probs={"start": 0.95},
        mass=0.95,
        top=[("start", 0.95)],
        p1=0.95,
        p2=0.0,
        margin=0.95,
    )
    snap = fake_fsm.snapshot()

    action_id = await tee.run(vr, req, score, snap, prompt_version="p1")

    assert len(fake_fsm.submitted) == 1
    submitted = fake_fsm.submitted[0]
    assert submitted.request_id == action_id
    assert submitted.origin == "SLM"
    assert submitted.action == Action.START
    assert submitted.requires_confirmation is True
    assert "text" not in submitted.meta  # E2/E5 privacy rule: no utterance text!

    # Simulate NEED_CONFIRMATION event from Arbiter via bus
    fake_bus.queue_event(
        DecisionEvent(request_id=action_id, verdict="NEED_CONFIRMATION")
    )
    await asyncio.sleep(0.05)

    # Ensure speaker read back phrase
    assert any(call[0] == "READBACK_START" for call in fake_speaker.calls)


@pytest.mark.asyncio
async def test_tee_deenergize_has_no_confirmation():
    cfg = TeeConfig()
    fake_fsm = FakeFsm("RUNNING")
    fake_bus = FakeDecisionBus()
    fake_speaker = FakeSpeaker()
    fake_clock = FakeClock()

    tee = SlmTee(cfg, fake_fsm, fake_bus, fake_speaker, candidates=None, clock=fake_clock)

    vr = ValidationResult(
        outcome=Outcome.ACTION,
        reason="OK",
        label="stop",
        intent_class=IntentClass.DEENERGIZE,
        action=Action.STOP,
    )
    req = SlmRequest(
        request_id="req456",
        event_id="ev456",
        text="stop immediately",
        ingested_mono_ns=1000,
        admitted_mono_ns=2000,
        state_at_prompt="RUNNING",
    )
    score = IntentScore(
        label_probs={"stop": 0.98},
        mass=0.98,
        top=[("stop", 0.98)],
        p1=0.98,
        p2=0.0,
        margin=0.98,
    )

    action_id = await tee.run(vr, req, score, fake_fsm.snapshot(), prompt_version="p1")
    assert len(fake_fsm.submitted) == 1
    assert fake_fsm.submitted[0].requires_confirmation is False
