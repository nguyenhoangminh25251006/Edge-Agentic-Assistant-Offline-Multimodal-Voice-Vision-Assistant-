"""Unit tests for the pre-rendered audio prompt cache."""
import os
import sys
import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tts_engine.prompt_cache import PromptCache, normalize_text


@pytest.fixture
def populated_cache():
    cache = PromptCache()
    dummy_audio = np.ones(16000, dtype=np.int16) * 100
    cache.put("EMERGENCY_STOP", dummy_audio, 16000)
    cache.put("LINE_STARTED", dummy_audio, 16000)
    cache.put("DEFECT_DETECTED", dummy_audio, 16000)
    return cache


def test_normalize_text():
    assert normalize_text("Emergency Stop!") == "emergency stop"
    assert normalize_text("  Conveyor   belt  Started... ") == "conveyor belt started"
    assert normalize_text("WARNING: Motor OVERLOAD!!") == "warning motor overload"


def test_prompt_cache_loading(populated_cache):
    assert len(populated_cache._memory_cache) == 3


def test_prompt_cache_hits(populated_cache):
    # Exact key hit
    res = populated_cache.get("EMERGENCY_STOP")
    assert res is not None
    audio_data, sample_rate = res
    assert isinstance(audio_data, np.ndarray)
    assert audio_data.dtype == np.int16
    assert sample_rate == 16000
    assert len(audio_data) > 0

    # Normalized text hit
    res2 = populated_cache.get("emergency stop activated. conveyor halted.")
    assert res2 is not None

    # Alias hit
    res3 = populated_cache.get("emergency stop")
    assert res3 is not None
    assert np.array_equal(res[0], res3[0])

    res4 = populated_cache.get("conveyor started")
    assert res4 is not None

    res5 = populated_cache.get("pcb defect detected")
    assert res5 is not None


def test_prompt_cache_miss(populated_cache):
    # Dynamic text that should not match any cached prompt
    res = populated_cache.get("The current motor winding temperature is 42 degrees Celsius.")
    assert res is None
