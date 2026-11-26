"""test_supervisor.py — Circuit breaker, canary checks, and status events (U-8)."""

import pytest
from slm_pipeline.config import BreakerConfig
from slm_pipeline.contracts import IntentClass, TokenProb
from slm_pipeline.prompt import PromptBuilder
from slm_pipeline.scorer import LabelScorer, LabelSpec
from slm_pipeline.supervisor import SlmSupervisor
from slm_pipeline.tests.fakes import FakeClock, FakeEventSink, FakeLlm


@pytest.fixture
def supervisor_setup():
    cfg = BreakerConfig(
        failure_threshold=3,
        open_s_initial=1.0,
        open_s_max=4.0,
        canary_interval_s=5.0,
        canary_timeout_ms=1000.0,
        degraded_latency_ms=3000.0,
    )
    fake_client = FakeLlm()
    prompt_builder = PromptBuilder("Classifier system prompt")
    scorer = LabelScorer([LabelSpec(label="start", intent_class=IntentClass.ENERGIZE, action=None)])
    fake_sink = FakeEventSink()
    fake_clock = FakeClock()

    sup = SlmSupervisor(
        cfg=cfg,
        client=fake_client,
        prompt_builder=prompt_builder,
        scorer=scorer,
        sink=fake_sink,
        clock=fake_clock,
        enabled=True,
    )
    return sup, fake_sink, fake_client


def test_three_failures_open_breaker(supervisor_setup):
    sup, sink, _ = supervisor_setup
    assert sup.breaker == "CLOSED"
    assert sup.status.state == "UP"

    # 1st failure
    sup.report_failure("TIMEOUT")
    assert sup.breaker == "CLOSED"

    # 2nd failure
    sup.report_failure("TIMEOUT")
    assert sup.breaker == "CLOSED"

    # 3rd failure opens breaker
    sup.report_failure("TIMEOUT")
    assert sup.breaker == "OPEN"
    assert sup.status.state == "DOWN"


@pytest.mark.asyncio
async def test_canary_probe_success_and_failure(supervisor_setup):
    sup, _, fake_client = supervisor_setup

    # Success: returns 'start' with p1 >= 0.5
    fake_client.scored_tokens = [TokenProb(token="start", prob=0.88)]
    ok = await sup.run_canary()
    assert ok is True

    # Failure: returns 'stop' instead of 'start'
    fake_client.scored_tokens = [TokenProb(token="stop", prob=0.90)]
    bad = await sup.run_canary()
    assert bad is False


def test_degraded_state_on_high_latency(supervisor_setup):
    sup, _, _ = supervisor_setup

    # Record 10 slow latencies above degraded_latency_ms (3000ms)
    for _ in range(10):
        sup.report_success(3500.0)

    assert sup.status.state == "DEGRADED"
