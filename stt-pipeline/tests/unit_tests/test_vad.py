"""VAD test suite: Voice Activity Detection session lifecycle and future calibration placeholders."""
import math
import os
import struct
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from audio_buffer_vad import VADSession
from config import BYTES_PER_FRAME, SAMPLE_RATE


def _sine_pcm(freq_hz: float, duration_s: float, amplitude: float = 5000.0) -> bytes:
    """Generate pure sine wave PCM audio for deterministic signal testing."""
    n = int(SAMPLE_RATE * duration_s)
    samples = [
        int(max(-32768, min(32767, amplitude * math.sin(2.0 * math.pi * freq_hz * i / SAMPLE_RATE))))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def test_vad_session_lifecycle():
    """Verify VADSession initialization, start state, and reset."""
    vad = VADSession()
    assert not vad.active

    vad.start()
    assert vad.active
    assert not vad._speaking

    vad.abort()
    assert not vad.active
    assert len(vad._frames) == 0


def test_vad_ignores_steady_motor_hum():
    """Verify that steady 60 Hz conveyor motor noise does not false-trigger VAD."""
    vad = VADSession()
    vad.start()
    motor_hum = _sine_pcm(60.0, 2.0, amplitude=8000.0)
    triggered = False

    for i in range(0, len(motor_hum) - BYTES_PER_FRAME + 1, BYTES_PER_FRAME):
        out = vad.feed(motor_hum[i : i + BYTES_PER_FRAME])
        if vad._speaking or out is not None:
            triggered = True

    assert not triggered, "Adaptive noise-floor gate must reject continuous 60 Hz hum"


def test_vad_non_truncation_on_idle_timeout():
    """Verify that active speech is not prematurely truncated by idle pre-speech timeout."""
    vad = VADSession(enable_bandpass=False)
    vad.start()
    # Simulate an idle wait near SESSION_TIMEOUT_SECONDS
    vad._session_start = time.monotonic() - 4.99

    speech_chunk = _sine_pcm(800.0, 0.02, amplitude=6000.0)
    vad._speaking = True
    vad._voiced_count = 10
    vad._frames = [speech_chunk] * 10
    time.sleep(0.03)

    res = vad.feed(speech_chunk)
    assert res is None and vad.active, "Active speaking session must not abort early due to idle timer"


@pytest.mark.skip(reason="Will be added later: Multi-SNR acoustic chamber VAD sweep")
def test_vad_dynamic_snr_threshold_sweep():
    """Placeholder: sweep factory SNR from -5 dB to +20 dB to map boundary trigger curves."""
    pass


@pytest.mark.skip(reason="Will be added later: Multichannel microphone array beamforming VAD")
def test_multichannel_beamforming_vad():
    """Placeholder: test spatial directional speech gating with multichannel microphone input."""
    pass
