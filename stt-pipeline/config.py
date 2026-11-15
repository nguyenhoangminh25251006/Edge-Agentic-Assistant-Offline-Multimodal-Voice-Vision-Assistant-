"""Configuration settings and parameter thresholds for the voice pipeline."""
import os
import platform
from pathlib import Path

# Base directory for the pipeline package
PIPELINE_DIR = Path(__file__).resolve().parent

# Model configuration paths
VOSK_MODEL_PATH = os.getenv(
    "VOSK_MODEL_PATH",
    os.getenv("PROD_VOSK_MODEL_PATH", os.path.expanduser("~/.cache/vosk/vosk-model-small-en-us-0.15")),
)


def get_vosk_model_path() -> str:
    """Return the active Vosk model path."""
    return VOSK_MODEL_PATH


# Audio capture specifications (16 kHz, 16-bit mono PCM)
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 20
BYTES_PER_FRAME = int(SAMPLE_RATE * (FRAME_MS / 1000.0) * 2)

# Speech-to-text & Adaptive VAD parameters (tuned for industrial conveyor floors)
SESSION_TIMEOUT_SECONDS = 5.0
MAX_SPEECH_SECONDS = 5.0
VAD_AGGRESSIVENESS = 2
VAD_HANGOVER_FRAMES = 20       # 20 * 20 ms = 400 ms (saves 100 ms vs 500 ms without clipping trailing words)
VAD_PRE_SPEECH_FRAMES = 20     # 20 * 20 ms = 400 ms (preserves soft initial fricatives /f/, /p/, /s/)
VAD_ONSET_WINDOW = 5
VAD_ONSET_MIN_FRAMES = 3       # 3/5 voiced frames required before opening capture window
VAD_MIN_VOICED_FRAMES = 5      # >= 100 ms active speech required to suppress metallic clicks
VAD_MIN_RMS = 120.0            # Absolute floor (~ -48 dBFS) ignoring near-silent ambient hum
VAD_SNR_GATE_RATIO = 1.30      # Frame RMS must exceed 1.30x adaptive noise floor (~ +2.3 dB)

# Spectral noise enhancement (engaged only when estimated utterance SNR drops below threshold)
ENABLE_BANDPASS = True
ENABLE_WIENER = True
WIENER_SNR_ENGAGE_DB = 9.0
WIENER_ALPHA = 1.05
WIENER_FLOOR = 0.25

# Inter-process communication endpoint
if platform.system() == "Windows":
    ZMQ_ENDPOINT = os.getenv("ZMQ_ENDPOINT", "tcp://127.0.0.1:5555")
else:
    ZMQ_ENDPOINT = os.getenv("ZMQ_ENDPOINT", "ipc:///tmp/ipc/stt_events.ipc")
