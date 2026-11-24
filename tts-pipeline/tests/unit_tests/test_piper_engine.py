"""Unit tests for the Piper neural TTS engine."""
import os
import sys
import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from tts_engine.piper_engine import PiperEngine


@pytest.fixture(scope="module")
def engine():
    return PiperEngine()


def test_piper_engine_initialization(engine):
    assert engine.sample_rate == 16000
    assert engine.model_path.endswith(".onnx")


def test_piper_engine_empty_text(engine):
    audio, rate, synth_time, rtf = engine.synthesize("")
    assert len(audio) == 0
    assert rate == 16000
    assert synth_time == 0.0


def test_piper_engine_synthesize_phrase(engine):
    text = "Conveyor running at 120 millimeters per second."
    audio, rate, synth_time, rtf = engine.synthesize(text)

    assert isinstance(audio, np.ndarray)
    assert audio.dtype == np.int16
    assert rate == 16000
    assert len(audio) > 0

    duration_s = len(audio) / rate
    assert duration_s > 1.5  # Spoken phrase is roughly 2-3 seconds long
    assert synth_time > 0.0
    assert rtf > 0.0
