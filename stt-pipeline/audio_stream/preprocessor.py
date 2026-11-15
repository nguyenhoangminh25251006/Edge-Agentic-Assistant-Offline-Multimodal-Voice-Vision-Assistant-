"""Dependency-light industrial audio conditioning: 150-7200Hz bandpass and SNR-gated Wiener denoiser."""
import struct
from typing import List

import numpy as np

try:
    from config import WIENER_ALPHA, WIENER_FLOOR, WIENER_SNR_ENGAGE_DB
except ImportError:
    from ..config import WIENER_ALPHA, WIENER_FLOOR, WIENER_SNR_ENGAGE_DB

# Pre-computed exact float64 4th-order Butterworth bandpass (150 Hz - 7,200 Hz @ 16 kHz, fs=16000)
# Eliminates heavy 60/120 Hz conveyor motor hum without shipping SciPy (~130 MB saved in container).
_BP_SOS: List[List[float]] = [
    [0.6121432556976094, 1.2242865113952188, 0.6121432556976094, 1.0, 1.4764321440709756, 0.5536713038990306],
    [1.0, 2.0, 1.0, 1.0, 1.7019389332495227, 0.7897668373709267],
    [1.0, -2.0, 1.0, 1.0, -1.8928433843316803, 0.896185153389155],
    [1.0, -2.0, 1.0, 1.0, -1.952818800225237, 0.9562207932548513],
]
_BP_ZI: List[List[float]] = [
    [0.19593906387825846, 0.16473126416028547],
    [0.11763407540236974, 0.07698221001148957],
    [-0.9257163949782146, 0.9257163949782169],
    [0.0, 0.0],
]


class AudioPreprocessor:
    """Streaming causal 150-7,200 Hz IIR bandpass filter preserving filter state across 20 ms chunks."""

    def __init__(self) -> None:
        self._zi: List[List[float]] = [[0.0, 0.0] for _ in _BP_SOS]
        self._initialized: bool = False

    def reset(self) -> None:
        """Reset biquad filter state when starting a new capture stream."""
        self._zi = [[0.0, 0.0] for _ in _BP_SOS]
        self._initialized = False

    def process(self, pcm_bytes: bytes) -> bytes:
        """Filter raw 16-bit PCM bytes through the 4-stage DF-II-T SOS bandpass filter."""
        n = len(pcm_bytes) // 2
        if n == 0:
            return pcm_bytes

        samples = list(struct.unpack(f"<{n}h", pcm_bytes))
        if not self._initialized:
            x0 = float(samples[0])
            self._zi = [[z0 * x0, z1 * x0] for z0, z1 in _BP_ZI]
            self._initialized = True

        buf: List[float] = [float(v) for v in samples]
        for s_idx, (b0, b1, b2, _, a1, a2) in enumerate(_BP_SOS):
            z0, z1 = self._zi[s_idx]
            out = [0.0] * n
            for i, xi in enumerate(buf):
                yi = b0 * xi + z0
                z0 = b1 * xi - a1 * yi + z1
                z1 = b2 * xi - a2 * yi
                out[i] = yi
            buf = out
            self._zi[s_idx] = [z0, z1]

        clamped = [max(-32768, min(32767, int(round(v)))) for v in buf]
        return struct.pack(f"<{n}h", *clamped)


def enhance_noisy_utterance(
    pcm_bytes: bytes,
    alpha: float = WIENER_ALPHA,
    floor: float = WIENER_FLOOR,
    snr_engage_db: float = WIENER_SNR_ENGAGE_DB,
) -> bytes:
    """Apply spectral Wiener denoising only when estimated utterance SNR drops below snr_engage_db."""
    n_samples = len(pcm_bytes) // 2
    n_fft = 512
    hop = 256
    if n_samples < n_fft:
        return pcm_bytes

    x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    win = np.hanning(n_fft)
    pad_len = (hop - (len(x) - n_fft) % hop) % hop
    x_pad = np.pad(x, (0, max(n_fft, pad_len)))
    n_frames = 1 + (len(x_pad) - n_fft) // hop

    frames = np.lib.stride_tricks.sliding_window_view(x_pad, n_fft)[::hop] * win
    specs = np.fft.rfft(frames, axis=1)
    power = np.abs(specs) ** 2

    frame_energy = np.sum(power, axis=1)
    k_slice = max(3, int(0.20 * len(frame_energy)))
    sorted_e = np.sort(frame_energy)
    quiet_e = float(np.mean(sorted_e[:k_slice])) + 1e-9
    loud_e = float(np.mean(sorted_e[-k_slice:])) + 1e-9
    est_snr_db = 10.0 * np.log10(loud_e / quiet_e)

    # Preserve bit-exact PCM in clean and mild-noise rooms so acoustic MFCCs are never distorted
    if est_snr_db >= snr_engage_db:
        return pcm_bytes

    quiet_idx = np.argsort(frame_energy)[:k_slice]
    noise_psd = np.mean(power[quiet_idx], axis=0, keepdims=True) + 1e-9
    gain = np.maximum(floor, 1.0 - alpha * (noise_psd / (power + 1e-9)))
    out_frames = np.fft.irfft(specs * gain, axis=1) * win

    y = np.zeros_like(x_pad)
    norm = np.zeros_like(x_pad) + 1e-9
    win_sq = win ** 2
    for i in range(n_frames):
        s = i * hop
        y[s : s + n_fft] += out_frames[i]
        norm[s : s + n_fft] += win_sq

    enhanced = np.clip((y / norm)[:n_samples], -32768, 32767).astype(np.int16)
    return enhanced.tobytes()
