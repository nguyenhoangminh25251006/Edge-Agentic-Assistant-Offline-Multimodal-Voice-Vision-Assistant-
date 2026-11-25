"""Python binding of the MCU's MFCC front end (firmware/src/voice/kws_frontend.c).

The C file is compiled to a host DLL with zig cc (shipped in the Zephyr venv as
the `ziglang` package) so training features are bit-for-bit the firmware's
portable-FFT features (the CMSIS-DSP FFT on the MCU differs by ~1e-6).
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "pico2" / "firmware" / "src" / "voice" / "kws_frontend.c"
DLL = HERE / "build" / ("kws_fe.dll" if os.name == "nt" else "libkws_fe.so")

SR, FRAME, HOP, NMFCC, NFRAMES, WINDOW = 16000, 480, 320, 10, 49, 16000


def _zig_python():
    zp = Path(os.environ.get("USERPROFILE", "~")).expanduser() / "zephyrproject" / ".venv" / "Scripts" / "python.exe"
    return str(zp) if zp.exists() else sys.executable


def build(force=False):
    if DLL.exists() and not force and DLL.stat().st_mtime > SRC.stat().st_mtime:
        return
    DLL.parent.mkdir(exist_ok=True)
    cmd = [_zig_python(), "-m", "ziglang", "cc", "-O2", "-shared", "-DKWS_FE_DLL",
           "-I", str(SRC.parent), "-o", str(DLL), str(SRC)]
    subprocess.check_call(cmd)


build()
_lib = ctypes.CDLL(str(DLL))
_lib.kws_fe_init()
_lib.kws_fe_clip.restype = ctypes.c_int
_lib.kws_fe_clip.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                             ctypes.POINTER(ctypes.c_float), ctypes.c_int]
_lib.kws_fe_frame.restype = ctypes.c_float
_lib.kws_fe_frame.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.POINTER(ctypes.c_float)]


def mfcc(pcm16, max_frames=None):
    """int16 PCM (any length) -> (frames, 10) float32, the MCU's features."""
    pcm16 = np.ascontiguousarray(pcm16, dtype=np.int16)
    n = len(pcm16)
    nf = max(0, 1 + (n - FRAME) // HOP)
    if max_frames is not None:
        nf = min(nf, max_frames)
    out = np.zeros((nf, NMFCC), np.float32)
    if nf:
        got = _lib.kws_fe_clip(pcm16.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)), n,
                               out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), nf)
        assert got == nf
    return out


def frame_db(frame16):
    """Energy (dBFS) of one 480-sample frame, as the MCU's VAD sees it."""
    frame16 = np.ascontiguousarray(frame16, dtype=np.int16)
    tmp = np.zeros(NMFCC, np.float32)
    return _lib.kws_fe_frame(frame16.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                             tmp.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))


def reference_mfcc(pcm16):
    """Independent numpy implementation, only used to unit-test the C code."""
    x = pcm16.astype(np.float64) / 32768.0
    k = np.arange(FRAME)
    win = 0.5 - 0.5 * np.cos(2 * np.pi * k / FRAME)
    nf = 1 + (len(x) - FRAME) // HOP
    frames = np.stack([x[i * HOP:i * HOP + FRAME] * win for i in range(nf)])
    pw = np.abs(np.fft.rfft(frames, 512)) ** 2
    hz2mel = lambda f: 2595 * np.log10(1 + f / 700)
    mel2hz = lambda m: 700 * (10 ** (m / 2595) - 1)
    edges = mel2hz(np.linspace(hz2mel(20), hz2mel(8000), 42))
    f = np.arange(257) * SR / 512
    fb = np.zeros((40, 257))
    for b in range(40):
        lo, c, hi = edges[b], edges[b + 1], edges[b + 2]
        fb[b] = np.clip(np.minimum((f - lo) / (c - lo), (hi - f) / (hi - c)), 0, None)
        fb[b][(f < lo) | (f >= hi)] = 0
    mel = np.log(pw @ fb.T + 1e-6)
    m = np.arange(10)[:, None]
    kk = np.arange(40)[None, :]
    d = np.sqrt(2 / 40) * np.cos(np.pi * m * (kk + 0.5) / 40)
    d[0] = np.sqrt(1 / 40)
    return (mel @ d.T).astype(np.float32)


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    t = np.arange(16000) / SR
    sig = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(16000)) * 32767
    pcm = np.clip(sig, -32768, 32767).astype(np.int16)
    a, b = mfcc(pcm), reference_mfcc(pcm)
    print("frames", a.shape, "max |C - numpy| =", float(np.abs(a - b).max()))
    assert a.shape == (NFRAMES, NMFCC) and np.abs(a - b).max() < 1e-2
    print("front end OK")
