"""Model engine test suite: STT inference contracts and future model benchmark placeholders."""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from config import get_vosk_model_path
from intent_parser import IntentParser
from stt_engine import BaseSTTEngine, TranscriptResult, VoskSTTEngine


def test_prod_model_path_configured():
    """Verify that the production Vosk model path is configured and exists on disk."""
    model_path = get_vosk_model_path()
    assert model_path and os.path.isdir(model_path), f"Expected valid production Vosk model dir, got {model_path}"


def test_model_engine_interface_contract():
    """Verify that VoskSTTEngine adheres to BaseSTTEngine and TranscriptResult."""
    model_path = get_vosk_model_path()
    engine = VoskSTTEngine(model_path=model_path, grammar=IntentParser.all_grammar_phrases())

    assert isinstance(engine, BaseSTTEngine)
    # Empty audio input must return conforming TranscriptResult with zero confidence
    res = engine.transcribe(b"")
    assert isinstance(res, dict)
    assert "text" in res
    assert "confidence" in res
    assert res["text"] == ""
    assert res["confidence"] == 0.0


@pytest.mark.skip(reason="Will be added later: Custom Edge Impulse / ONNX model weight benchmark")
def test_custom_acoustic_model_accuracy():
    """Placeholder: benchmark custom-trained acoustic model weights on industrial speech dataset."""
    pass


@pytest.mark.skip(reason="Will be added later: INT8 quantization drift and SNR stress profiling")
def test_acoustic_model_quantization_drift():
    """Placeholder: compare full-precision vs quantized model inference accuracy under factory noise."""
    pass
