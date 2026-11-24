"""Piper neural TTS engine wrapper (ONNX Runtime CPU)."""
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np

try:
    import onnxruntime as ort
    from piper import PiperVoice
    from piper.config import PiperConfig
except ImportError:
    ort = None
    PiperVoice = None
    PiperConfig = None

try:
    from .base import BaseTTSEngine
    from config import get_piper_config_path, get_piper_model_path
except ImportError:
    from .base import BaseTTSEngine
    from ..config import get_piper_config_path, get_piper_model_path


def _resolve_ort_threads(explicit: Optional[int] = None) -> Tuple[int, bool]:
    """Resolve ONNX Runtime intra-op thread count and whether execution is cgroup-constrained.

    Respects explicit argument, ORT_INTRA_OP_NUM_THREADS, OMP_NUM_THREADS, Linux CPU
    affinity (cpuset), and cgroup v2 cpu.max quotas so ONNX Runtime never oversubscribes
    or spin-thrashes inside containerized edge deployments (UNO Q / Raspberry Pi).
    """
    constrained = False
    if explicit is not None and explicit > 0:
        return explicit, True

    for env_key in ("ORT_INTRA_OP_NUM_THREADS", "OMP_NUM_THREADS"):
        val = os.getenv(env_key)
        if val and val.isdigit() and int(val) > 0:
            return int(val), True

    avail = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        try:
            aff = len(os.sched_getaffinity(0))
            if aff < avail:
                constrained = True
            avail = min(avail, aff)
        except OSError:
            pass

    # Check Linux cgroup v2 CPU quota (e.g. "20000 100000" -> 0.20 cores -> 1 thread)
    cgroup_cpu_max = Path("/sys/fs/cgroup/cpu.max")
    if cgroup_cpu_max.is_file():
        try:
            parts = cgroup_cpu_max.read_text(encoding="utf-8").strip().split()
            if len(parts) == 2 and parts[0] != "max":
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    eff_cores = quota / period
                    constrained = True
                    avail = min(avail, max(1, int(math.floor(eff_cores + 1e-6)) or 1))
        except (OSError, ValueError):
            pass

    return max(1, min(avail, 4)), constrained


class PiperEngine(BaseTTSEngine):
    """Local, offline neural text-to-speech engine powered by Piper."""

    def __init__(
        self,
        model_path: Optional[Union[str, Path]] = None,
        config_path: Optional[Union[str, Path]] = None,
        use_cuda: bool = False,
        intra_op_threads: Optional[int] = None,
    ) -> None:
        if PiperVoice is None:
            raise ImportError(
                "The 'piper-tts' package is required. Install via pip install piper-tts"
            )

        self._model_path = str(model_path or get_piper_model_path())
        self._config_path = str(config_path or get_piper_config_path())

        if not os.path.isfile(self._model_path):
            raise FileNotFoundError(
                f"Piper ONNX model not found at '{self._model_path}'"
            )

        if not os.path.isfile(self._config_path):
            # Piper can often infer config if it's named <model>.json
            inferred_cfg = f"{self._model_path}.json"
            if os.path.isfile(inferred_cfg):
                self._config_path = inferred_cfg
            else:
                raise FileNotFoundError(
                    f"Piper JSON config not found at '{self._config_path}'"
                )

        self._lock = threading.Lock()
        if not use_cuda and ort is not None and PiperConfig is not None:
            num_threads, _ = _resolve_ort_threads(intra_op_threads)
            sess_opts = ort.SessionOptions()
            sess_opts.intra_op_num_threads = num_threads
            sess_opts.inter_op_num_threads = 1
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # Disable thread-pool busy-wait spinning so idle standby CPU stays < 0.5%
            # and CFS-throttled containers never suffer spin-lock convoy slowdowns.
            sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            sess_opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
            with open(self._config_path, "r", encoding="utf-8") as cfg_file:
                cfg_dict = json.load(cfg_file)
            session = ort.InferenceSession(
                self._model_path,
                sess_options=sess_opts,
                providers=["CPUExecutionProvider"],
            )
            self._voice = PiperVoice(
                config=PiperConfig.from_dict(cfg_dict),
                session=session,
            )
        else:
            self._voice = PiperVoice.load(
                model_path=self._model_path,
                config_path=self._config_path,
                use_cuda=use_cuda,
            )
        self._sample_rate = int(self._voice.config.sample_rate)

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def model_path(self) -> str:
        return self._model_path

    def synthesize(self, text: str) -> Tuple[np.ndarray, int, float, float]:
        """Synthesize text into int16 PCM audio array.

        Returns:
            Tuple of:
                - audio_data: np.ndarray (int16 PCM, mono)
                - sample_rate: int (Hz)
                - synthesis_time_s: float
                - rtf: float (Real-Time Factor)
        """
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.int16), self._sample_rate, 0.0, 0.0

        start_time = time.perf_counter()
        with self._lock:
            chunks = list(self._voice.synthesize(text))

        synthesis_time = time.perf_counter() - start_time

        if not chunks:
            return np.zeros(0, dtype=np.int16), self._sample_rate, synthesis_time, 0.0

        audio_arrays = [c.audio_int16_array for c in chunks if c.audio_int16_array is not None]
        if not audio_arrays:
            return np.zeros(0, dtype=np.int16), self._sample_rate, synthesis_time, 0.0

        audio_data = np.concatenate(audio_arrays)
        duration_s = len(audio_data) / self._sample_rate
        rtf = synthesis_time / duration_s if duration_s > 0 else 0.0

        return audio_data, self._sample_rate, synthesis_time, rtf
