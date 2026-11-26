"""test_admission.py — Inbound admission control, dedupe, rate limiting, and cancellation (U-1)."""

import asyncio
import pytest

from control_pipeline.schemas.fsm import Action
from slm_pipeline.admission import SlmAdmission
from slm_pipeline.config import SlmConfig
from slm_pipeline.contracts import (
    IntentClass,
    Outcome,
    SlmResult,
    TokenProb,
    UtteranceUnmatched,
)
from slm_pipeline.prompt import PromptBuilder
from slm_pipeline.scorer import LabelScorer, LabelSpec
from slm_pipeline.supervisor import SlmSupervisor
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


@pytest.fixture
def admission_setup():
    cfg = SlmConfig.default()
    cfg.enabled = True
    cfg.max_total_ms = 4000.0

    fake_clock = FakeClock(1_000_000_000)
    fake_fsm = FakeFsm("IDLE")
    fake_bus = FakeDecisionBus()
    fake_speaker = FakeSpeaker()
    fake_sink = FakeEventSink()
    fake_client = FakeLlm()

    prompt_builder = PromptBuilder("Classifier prompt")
    labels = [
        LabelSpec(label="start", intent_class=IntentClass.ENERGIZE, action=Action.START),
        LabelSpec(label="stop", intent_class=IntentClass.DEENERGIZE, action=Action.STOP),
        LabelSpec(label="none", intent_class=IntentClass.META, action=None),
    ]
    scorer = LabelScorer(labels)
    validator = OutputValidator(cfg.thresholds, scorer.label_map, cfg.speed, cfg.max_total_ms)
    supervisor = SlmSupervisor(cfg.breaker, fake_client, prompt_builder, scorer, fake_sink, fake_clock, enabled=True)
    tee = SlmTee(cfg.tee, fake_fsm, fake_bus, fake_speaker, candidates=None, clock=fake_clock)

    admission = SlmAdmission(
        cfg=cfg,
        fsm=fake_fsm,
        supervisor=supervisor,
        prompt_builder=prompt_builder,
        client=fake_client,
        scorer=scorer,
        validator=validator,
        tee=tee,
        speaker=fake_speaker,
        sink=fake_sink,
        clock=fake_clock,
    )
    return admission, fake_sink, fake_clock, fake_client


@pytest.mark.asyncio
async def test_admission_wake_not_confirmed(admission_setup):
    admission, sink, clock, _ = admission_setup

    ev = UtteranceUnmatched(
        event_id="ev1",
        text="start the belt",
        ingested_mono_ns=clock.now_mono_ns(),
        wake_confirmed=False,
    )
    await admission.on_unmatched(ev)

    assert len(sink.events) == 1
    res = sink.events[0]
    assert isinstance(res, SlmResult)
    assert res.outcome == Outcome.NO_COMMAND
    assert res.reason == "NOT_WAKE_CONFIRMED"


@pytest.mark.asyncio
async def test_admission_stale_input(admission_setup):
    admission, sink, clock, _ = admission_setup

    # Age 5000 ms > max_total_ms (4000 ms)
    old_time_ns = clock.now_mono_ns() - 5_000_000_000
    ev = UtteranceUnmatched(
        event_id="ev_stale",
        text="start the belt",
        ingested_mono_ns=old_time_ns,
        wake_confirmed=True,
    )
    await admission.on_unmatched(ev)

    assert len(sink.events) == 1
    res = sink.events[0]
    assert isinstance(res, SlmResult)
    assert res.outcome == Outcome.TIMEOUT
    assert res.reason == "STALE_INPUT"


@pytest.mark.asyncio
async def test_admission_dedupe(admission_setup):
    admission, sink, clock, _ = admission_setup

    ev = UtteranceUnmatched(
        event_id="ev_dup",
        text="start the belt",
        ingested_mono_ns=clock.now_mono_ns(),
        wake_confirmed=True,
    )
    await admission.on_unmatched(ev)
    await asyncio.sleep(0.05)
    first_count = len(sink.events)

    # Second arrival of same event_id
    await admission.on_unmatched(ev)
    assert len(sink.events) == first_count


@pytest.mark.asyncio
async def test_admission_matched_command_cancels(admission_setup):
    admission, sink, clock, fake_client = admission_setup

    fake_client.delay_s = 0.5  # Artificial delay

    ev = UtteranceUnmatched(
        event_id="ev_cancel",
        text="start the belt",
        ingested_mono_ns=clock.now_mono_ns(),
        wake_confirmed=True,
    )
    await admission.on_unmatched(ev)

    # Trigger matched command during inference
    await asyncio.sleep(0.05)
    admission.on_matched_command()

    await asyncio.sleep(0.1)
    # Ensure cancelled event published
    cancelled_results = [e for e in sink.events if isinstance(e, SlmResult) and e.outcome == Outcome.CANCELLED]
    assert len(cancelled_results) == 1
    assert cancelled_results[0].reason == "MATCHED_COMMAND"
