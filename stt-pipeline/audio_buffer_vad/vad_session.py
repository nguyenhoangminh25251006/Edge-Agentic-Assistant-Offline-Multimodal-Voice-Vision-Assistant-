"""Adaptive Voice Activity Detection (VAD) session management for conveyor speech capture."""
from collections import deque
import math
import struct
import time
from typing import Optional

import webrtcvad  # type: ignore

try:
    from audio_stream.preprocessor import AudioPreprocessor
    from config import (
        ENABLE_BANDPASS,
        FRAME_MS,
        MAX_SPEECH_SECONDS,
        SAMPLE_RATE,
        SESSION_TIMEOUT_SECONDS,
        VAD_AGGRESSIVENESS,
        VAD_HANGOVER_FRAMES,
        VAD_MIN_RMS,
        VAD_MIN_VOICED_FRAMES,
        VAD_ONSET_MIN_FRAMES,
        VAD_ONSET_WINDOW,
        VAD_PRE_SPEECH_FRAMES,
        VAD_SNR_GATE_RATIO,
    )
except ImportError:
    from ..audio_stream.preprocessor import AudioPreprocessor
    from ..config import (
        ENABLE_BANDPASS,
        FRAME_MS,
        MAX_SPEECH_SECONDS,
        SAMPLE_RATE,
        SESSION_TIMEOUT_SECONDS,
        VAD_AGGRESSIVENESS,
        VAD_HANGOVER_FRAMES,
        VAD_MIN_RMS,
        VAD_MIN_VOICED_FRAMES,
        VAD_ONSET_MIN_FRAMES,
        VAD_ONSET_WINDOW,
        VAD_PRE_SPEECH_FRAMES,
        VAD_SNR_GATE_RATIO,
    )


def _compute_rms(chunk: bytes) -> float:
    """Calculate Root Mean Square amplitude of a 16-bit signed mono PCM chunk."""
    count = len(chunk) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", chunk)
    return math.sqrt(sum(s * s for s in samples) / count)


def _percentile_25(values: deque[float]) -> float:
    """Compute the 25th percentile noise-floor estimate without external sorting overhead."""
    ordered = sorted(values)
    idx = max(0, (len(ordered) - 1) // 4)
    return ordered[idx]


class VADSession:
    """Manages an active speech capture window using bandpass filtering, adaptive noise-floor
    minimum statistics gating, and WebRTC Voice Activity Detection.
    """

    def __init__(self, enable_bandpass: bool = ENABLE_BANDPASS) -> None:
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._preprocessor: Optional[AudioPreprocessor] = (
            AudioPreprocessor() if enable_bandpass else None
        )
        self._pre_buffer: deque[bytes] = deque(maxlen=VAD_PRE_SPEECH_FRAMES)
        self._onset_history: deque[int] = deque(maxlen=VAD_ONSET_WINDOW)
        self._rms_history: deque[float] = deque(maxlen=75)  # ~1.5 s sliding noise floor
        self._frames: list[bytes] = []
        self._speaking: bool = False
        self._silence_count: int = 0
        self._voiced_count: int = 0
        self._session_start: float = 0.0
        self._active: bool = False

    def reset_noise_floor(self) -> None:
        """Reset adaptive background noise estimator and bandpass state (for clip/file harnesses)."""
        self._rms_history.clear()
        if self._preprocessor is not None:
            self._preprocessor.reset()

    def start(self) -> None:
        """Start a new speech capture session while preserving adaptive noise baseline."""
        self._speaking = False
        self._silence_count = 0
        self._voiced_count = 0
        self._frames = []
        self._pre_buffer.clear()
        self._onset_history.clear()
        self._session_start = time.monotonic()
        self._active = True

    def feed(self, chunk: bytes) -> Optional[bytes]:
        """Feed a 20 ms 16 kHz mono PCM frame into the session.

        Returns filtered utterance PCM bytes when speech completes or max speech duration
        is reached; returns None otherwise.
        """
        if not self._active:
            return None

        if self._preprocessor is not None:
            chunk = self._preprocessor.process(chunk)

        frame_rms = _compute_rms(chunk)

        # Track ambient floor only outside active speech (plus tiny warmup window)
        if not self._speaking or len(self._rms_history) < 10:
            self._rms_history.append(frame_rms)

        noise_floor = _percentile_25(self._rms_history) if len(self._rms_history) >= 5 else frame_rms
        rms_gate = max(VAD_MIN_RMS, VAD_SNR_GATE_RATIO * noise_floor)

        try:
            webrtc_speech = self._vad.is_speech(chunk, SAMPLE_RATE)
        except Exception:
            webrtc_speech = False

        is_speech = webrtc_speech and (frame_rms >= rms_gate)

        if not self._speaking:
            # Expire idle window cleanly only when waiting in silence (never truncating mid-word)
            if time.monotonic() - self._session_start >= SESSION_TIMEOUT_SECONDS:
                self._active = False
                return None

            self._pre_buffer.append(chunk)
            self._onset_history.append(1 if is_speech else 0)
            if sum(self._onset_history) >= VAD_ONSET_MIN_FRAMES:
                self._speaking = True
                self._frames = list(self._pre_buffer)
                self._silence_count = 0
                self._voiced_count = sum(self._onset_history)
            return None

        # Active speech state: enforce duration by captured sample count rather than wall idle time
        self._frames.append(chunk)
        captured_seconds = len(self._frames) * (FRAME_MS / 1000.0)
        if captured_seconds >= MAX_SPEECH_SECONDS:
            self._active = False
            return b"".join(self._frames) if self._voiced_count >= VAD_MIN_VOICED_FRAMES else None

        if is_speech:
            self._silence_count = 0
            self._voiced_count += 1
        else:
            self._silence_count += 1
            if self._silence_count >= VAD_HANGOVER_FRAMES:
                self._active = False
                if self._voiced_count >= VAD_MIN_VOICED_FRAMES:
                    return b"".join(self._frames)
                return None

        return None

    @property
    def active(self) -> bool:
        """Check whether the session is currently active."""
        return self._active

    def abort(self) -> None:
        """Abort the current session and clear buffered frames."""
        self._active = False
        self._speaking = False
        self._frames = []
        self._pre_buffer.clear()
        self._onset_history.clear()
