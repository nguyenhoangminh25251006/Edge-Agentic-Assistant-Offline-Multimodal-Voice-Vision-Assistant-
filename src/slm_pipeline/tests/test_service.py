"""test_service.py — Startup wiring, resource checks, and lifecycle tests (U-10)."""

import pytest
from slm_pipeline.config import SlmConfig
from slm_pipeline.service import SlmService
from slm_pipeline.tests.fakes import (
    FakeClock,
    FakeDecisionBus,
    FakeEventSink,
    FakeFsm,
    FakeLlm,
    FakeSpeaker,
)


@pytest.mark.asyncio
async def test_service_disabled_by_default():
    cfg = SlmConfig.default()
    assert cfg.enabled is False

    service = SlmService(cfg=cfg)
    ok = await service.initialize()
    assert ok is True
    # When disabled, components stay uninitialized
    assert service.admission is None


@pytest.mark.asyncio
async def test_service_startup_wiring_success():
    cfg = SlmConfig.default()
    cfg.enabled = True

    fake_clock = FakeClock()
    fake_fsm = FakeFsm("IDLE")
    fake_bus = FakeDecisionBus()
    fake_speaker = FakeSpeaker()
    fake_sink = FakeEventSink()
    fake_client = FakeLlm()

    service = SlmService(
        cfg=cfg,
        fsm=fake_fsm,
        bus=fake_bus,
        speaker=fake_speaker,
        sink=fake_sink,
        clock=fake_clock,
        client=fake_client,
    )

    ok = await service.initialize()
    assert ok is True
    assert service.admission is not None
    assert service.supervisor is not None
    assert service.supervisor.status.state in ("UP", "DOWN")

    await service.stop()
