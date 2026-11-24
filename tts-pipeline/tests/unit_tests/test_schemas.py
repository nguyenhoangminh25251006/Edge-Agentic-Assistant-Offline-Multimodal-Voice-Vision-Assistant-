"""Unit tests for TTS Pydantic schemas and serialization."""
import json
import os
import sys
import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent


def test_priority_enum_values():
    assert PriorityEnum.CRITICAL == 0
    assert PriorityEnum.HIGH == 1
    assert PriorityEnum.NORMAL == 2
    assert PriorityEnum.LOW == 3
    # Critical has higher priority (smaller numerical value) than Normal
    assert PriorityEnum.CRITICAL < PriorityEnum.NORMAL


def test_tts_request_valid():
    req = TTSRequest(
        request_id="test_001",
        text="Conveyor started",
        priority=PriorityEnum.HIGH,
        preempt=True,
    )
    assert req.request_id == "test_001"
    assert req.text == "Conveyor started"
    assert req.priority == PriorityEnum.HIGH
    assert req.preempt is True
    assert req.use_cache_only is False
    assert req.timestamp > 0.0


def test_tts_request_defaults():
    req = TTSRequest(request_id="test_002", text="Status normal")
    assert req.priority == PriorityEnum.NORMAL
    assert req.preempt is False
    assert req.use_cache_only is False


def test_tts_request_empty_text_fails():
    with pytest.raises(ValidationError):
        TTSRequest(request_id="test_003", text="")


def test_tts_request_serialization_roundtrip():
    original = TTSRequest(
        request_id="req_123",
        text="Board jam on rail 2",
        priority=PriorityEnum.CRITICAL,
        preempt=True,
    )
    dumped = original.model_dump()
    json_str = json.dumps(dumped)
    loaded = json.loads(json_str)
    restored = TTSRequest.model_validate(loaded)

    assert restored.request_id == original.request_id
    assert restored.text == original.text
    assert restored.priority == original.priority
    assert restored.preempt == original.preempt


def test_tts_status_event():
    event = TTSStatusEvent(
        request_id="req_999",
        status=TTSStatusEnum.COMPLETED,
        cached=True,
        latency_ms=12.5,
        duration_s=2.34,
        rtf=0.0,
    )
    assert event.source == "container-tts"
    assert event.status == TTSStatusEnum.COMPLETED
    assert event.cached is True
    assert event.latency_ms == 12.5
    assert event.error_msg is None
