"""Configuration settings and parameters for the TTS (Text-to-Speech) pipeline.

Target Edge Platform: Arduino UNO Q (Qualcomm QRB2210 + STM32U585)
Voice Model: Piper en_US-lessac-low (ONNX Runtime CPU)
"""
import os
import platform
from pathlib import Path

# Base paths
PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent
MODELS_DIR = PROJECT_ROOT / "models"
TEST_INPUT_DIR = PIPELINE_DIR / "test_input"

# Piper model paths
DEFAULT_PIPER_MODEL = os.path.expanduser("~/.cache/piper/en_US-lessac-low.onnx")
DEFAULT_PIPER_CONFIG = os.path.expanduser("~/.cache/piper/en_US-lessac-low.onnx.json")

PIPER_MODEL_PATH = os.getenv("PIPER_MODEL_PATH", os.getenv("PROD_PIPER_MODEL_PATH", DEFAULT_PIPER_MODEL))
PIPER_CONFIG_PATH = os.getenv("PIPER_CONFIG_PATH", os.getenv("PROD_PIPER_CONFIG_PATH", DEFAULT_PIPER_CONFIG))


def get_piper_model_path() -> str:
    """Return the active Piper ONNX model path."""
    return PIPER_MODEL_PATH


def get_piper_config_path() -> str:
    """Return the active Piper JSON configuration path."""
    return PIPER_CONFIG_PATH


# Audio synthesis & playback parameters
DEFAULT_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM

# Performance and latency SLA thresholds (NFR-03, Table 20)
MAX_LATENCY_MS = 300.0        # End-to-end alert latency ceiling
TARGET_RTF = 0.28             # Maximum acceptable Real-Time Factor (synthesis_time / duration)

# Pre-rendered prompt configuration
ENABLE_PROMPT_CACHE = True

# Inter-process communication endpoints (ZeroMQ)
if platform.system() == "Windows":
    ZMQ_PULL_ENDPOINT = os.getenv("ZMQ_PULL_ENDPOINT", "tcp://127.0.0.1:5556")
    ZMQ_PUB_ENDPOINT = os.getenv("ZMQ_PUB_ENDPOINT", "tcp://127.0.0.1:5557")
else:
    ZMQ_PULL_ENDPOINT = os.getenv("ZMQ_PULL_ENDPOINT", "ipc:///tmp/ipc/tts_requests.ipc")
    ZMQ_PUB_ENDPOINT = os.getenv("ZMQ_PUB_ENDPOINT", "ipc:///tmp/ipc/tts_status.ipc")
