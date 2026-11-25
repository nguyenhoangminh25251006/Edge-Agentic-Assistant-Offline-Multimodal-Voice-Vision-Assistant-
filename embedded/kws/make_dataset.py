"""Build the "Hey Edgie" wake-word dataset (16 kHz mono int16, 1 s clips).

No recordings of the phrase exist yet, so positives are synthesised:
  * train/val : Piper TTS, en_US-libritts_r-medium (904 speakers), random speaker,
                speed and prosody noise;
  * test      : voices never seen in training (Piper lessac / amy / alan /
                northern_english_male + Windows SAPI David / Zira) to measure
                how the model generalises to a different speaker / engine.
Negatives:
  * Google Speech Commands v2 (35 words, ~2,600 real speakers, split by speaker),
  * TTS confusables ("hey Eddie", "edgy", "hey Reggie", "hedge", ...) and
    conveyor-room phrases ("start the belt", "stop", ...),
  * background noise (Speech Commands noise files + synthetic motor hum).
Output: data/clips/{split}/{label}/*.wav  plus data/noise/*.wav
"""
import argparse
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(os.environ.get("KWS_HOME", Path.home() / "kws"))
VOICES = ROOT / "voices"
DATA = ROOT / "data"
SC = DATA / "sc"
OUT = DATA / "clips"
NOISE = DATA / "noise"
SR = 16000

WAKE_TEXTS = ["hey edgy", "hey edgy.", "hey, edgy!", "hey edgie", "hey edgy?", "hey, edgie."]
CONFUSABLE = ["hey eddie", "hey reggie", "hey teddy", "edgy", "hey", "hey there", "hedge",
              "hey edge", "a veggie", "hey betty", "head", "heavy", "hey ed", "ready",
              "hey Siri", "hey Google", "okay edgy", "edgy hey", "hey, hey", "say edgy",
              "the edge", "hey Freddie", "pledge", "egg", "hey Jesse", "hey baby"]
FACTORY = ["start the belt", "stop the belt", "stop", "speed up", "slow down",
           "what is the speed", "acknowledge the alarm", "restart the line",
           "the motor is hot", "check the sensor", "turn it off", "hello",
           "good morning", "can you hear me", "where is the box", "set speed to two hundred",
           "it is jammed again", "emergency", "okay", "thank you", "one two three"]


def write_wav(path, pcm):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype(np.int16).tobytes())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), np.int16).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    if sr != SR:
        g = np.gcd(sr, SR)
        x = resample_poly(x, SR // g, sr // g)
    return x


def trim(x, thr_db=-40):
    """Cut leading/trailing silence (20 ms blocks below thr relative to peak)."""
    if len(x) == 0:
        return x
    peak = np.abs(x).max() + 1e-9
    blk = 320
    e = [20 * np.log10(np.abs(x[i:i + blk]).max() / peak + 1e-9) for i in range(0, len(x), blk)]
    on = [i for i, v in enumerate(e) if v > thr_db]
    if not on:
        return x[:0]
    return x[on[0] * blk:(on[-1] + 1) * blk]


def normalise(x, peak=0.6):
    m = np.abs(x).max()
    return x * (peak * 32767 / m) if m > 0 else x


class Piper:
    def __init__(self, name):
        from piper import PiperVoice
        self.voice = PiperVoice.load(str(VOICES / f"{name}.onnx"))
        cfg = json.loads((VOICES / f"{name}.onnx.json").read_text(encoding="utf-8"))
        self.sr = cfg["audio"]["sample_rate"]
        self.speakers = cfg.get("num_speakers", 1)

    def say(self, text, speaker=None, length=1.0, noise=0.667, noise_w=0.8):
        buf = io.BytesIO()
        try:  # piper-tts >= 1.3
            from piper import SynthesisConfig
            cfg = SynthesisConfig(speaker_id=speaker, length_scale=length,
                                  noise_scale=noise, noise_w_scale=noise_w)
            with wave.open(buf, "wb") as w:
                self.voice.synthesize_wav(text, w, syn_config=cfg)
        except ImportError:
            with wave.open(buf, "wb") as w:
                self.voice.synthesize(text, w, speaker_id=speaker, length_scale=length,
                                      noise_scale=noise, noise_w=noise_w)
        buf.seek(0)
        with wave.open(buf, "rb") as w:
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
            sr = w.getframerate()
        g = np.gcd(sr, SR)
        return resample_poly(x, SR // g, sr // g)


def sapi_say(text, voice, rate, path):
    """Windows SAPI voice (David / Zira) -> 16 kHz wav, via PowerShell."""
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SelectVoice('{voice}');$s.Rate={rate};"
          "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
          "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
          "[System.Speech.AudioFormat.AudioChannel]::Mono);"
          f"$s.SetOutputToWaveFile('{path}',$f);$s.Speak('{text}');$s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True)


def place(x, rng):
    """Put an utterance in a 1 s window. Positives end inside the last 0.5 s."""
    x = x[:SR]
    out = np.zeros(SR, np.float32)
    start = rng.integers(0, SR - len(x) + 1)
    out[start:start + len(x)] = x
    return out


def split_of(key, val=0.1, test=0.1):
    h = int(hashlib.md5(key.encode()).hexdigest(), 16) % 1000 / 1000
    return "test" if h < test else ("val" if h < test + val else "train")


def gen_tts(args, rng):
    lib = Piper("en_US-libritts_r-medium")
    n = 0
    print(f"libritts_r: {lib.speakers} speakers")
    for i in range(args.positives):
        if i % args.shards != args.shard:
            continue
        spk = int(rng.integers(lib.speakers))
        split = "val" if spk % 10 == 0 else "train"  # validation speakers held out
        x = trim(lib.say(random.choice(WAKE_TEXTS), spk, rng.uniform(0.8, 1.35),
                         rng.uniform(0.4, 0.9), rng.uniform(0.5, 1.0)))
        if 0.25 * SR < len(x) <= SR:
            write_wav(OUT / split / "wake" / f"lt_{i:05d}.wav", place(normalise(x), rng))
            n += 1
    print("positives", n)
    texts = CONFUSABLE * 3 + FACTORY
    for i in range(args.tts_negatives):
        if i % args.shards != args.shard:
            continue
        spk = int(rng.integers(lib.speakers))
        split = "val" if spk % 10 == 0 else "train"
        x = trim(lib.say(random.choice(texts), spk, rng.uniform(0.8, 1.3)))
        if len(x) > 0.1 * SR:
            # long phrases: keep a random 1 s piece (the model also sees partial words)
            if len(x) > SR:
                o = rng.integers(0, len(x) - SR)
                x = x[o:o + SR]
            write_wav(OUT / split / "unknown" / f"lt_{i:05d}.wav", place(normalise(x), rng))
    if args.shard != 0:
        return
    # held-out engines / voices: test only
    for name in ["en_US-lessac-medium", "en_US-amy-medium", "en_GB-alan-medium",
                 "en_GB-northern_english_male-medium"]:
        v = Piper(name)
        for j in range(40):
            x = trim(v.say(WAKE_TEXTS[j % len(WAKE_TEXTS)], None, rng.uniform(0.8, 1.3),
                           rng.uniform(0.4, 0.9)))
            if 0.25 * SR < len(x) <= SR:
                write_wav(OUT / "test" / "wake" / f"{name}_{j}.wav", place(normalise(x), rng))
            y = trim(v.say(random.choice(CONFUSABLE), None, rng.uniform(0.8, 1.3)))
            if len(y) > 0.1 * SR:
                write_wav(OUT / "test" / "unknown" / f"{name}_{j}.wav",
                          place(normalise(y[:SR]), rng))
    if os.name == "nt":
        tmp = DATA / "sapi_tmp.wav"
        for voice in ["Microsoft David Desktop", "Microsoft Zira Desktop"]:
            for j, rate in enumerate(range(-3, 4)):
                for k, text in enumerate(["hey edgy", "hey eddie", "hey reggie", "edgy"]):
                    sapi_say(text, voice, rate, tmp)
                    x = trim(read_wav(tmp))
                    lab = "wake" if k == 0 else "unknown"
                    if 0.1 * SR < len(x) <= SR:
                        write_wav(OUT / "test" / lab / f"sapi_{voice.split()[1]}_{j}_{k}.wav",
                                  place(normalise(x), rng))
        tmp.unlink(missing_ok=True)


EXTRA_VOICES = ["en_GB-vctk-medium", "en_US-l2arctic-medium", "en_US-arctic-medium",
                "en_US-ryan-medium", "en_US-hfc_female-medium", "en_US-hfc_male-medium",
                "en_GB-semaine-medium"]


def gen_extra(args, rng):
    """More speaker / accent diversity for training: VCTK (109 British-Isles
    speakers), L2-ARCTIC (24 non-native speakers: Arabic, Chinese, Hindi, Korean,
    Spanish, Vietnamese), ARCTIC, and single-speaker voices. Test voices stay unseen."""
    texts = CONFUSABLE * 3 + FACTORY
    for name in EXTRA_VOICES:
        v = Piper(name)
        n_spk = max(1, v.speakers)
        n_pos = args.extra_per_voice * (3 if n_spk > 1 else 1)
        for i in range(n_pos):
            if i % args.shards != args.shard:
                continue
            spk = int(rng.integers(n_spk)) if n_spk > 1 else None
            split = "val" if (spk or 0) % 10 == 0 and n_spk > 1 else "train"
            x = trim(v.say(random.choice(WAKE_TEXTS), spk, rng.uniform(0.8, 1.35),
                           rng.uniform(0.4, 0.9), rng.uniform(0.5, 1.0)))
            if 0.25 * SR < len(x) <= SR:
                write_wav(OUT / split / "wake" / f"{name}_{i:05d}.wav", place(normalise(x), rng))
            y = trim(v.say(random.choice(texts), spk, rng.uniform(0.8, 1.3)))
            if len(y) > 0.1 * SR:
                if len(y) > SR:
                    o = rng.integers(0, len(y) - SR)
                    y = y[o:o + SR]
                write_wav(OUT / split / "unknown" / f"{name}_{i:05d}.wav", place(normalise(y), rng))
        print(name, n_spk, "speakers done")


def gen_sc(args, rng):
    words = [d for d in sorted(os.listdir(SC)) if (SC / d).is_dir() and not d.startswith("_")]
    per_word = args.sc_per_word
    for w in words:
        files = sorted((SC / w).glob("*.wav"))
        rng.shuffle(files)
        for f in files[:per_word]:
            spk = f.stem.split("_nohash_")[0]
            split = split_of(spk)
            x = read_wav(f)
            x = np.pad(x, (0, max(0, SR - len(x))))[:SR]
            write_wav(OUT / split / "unknown" / f"sc_{w}_{f.stem}.wav", x)


def gen_noise(rng):
    NOISE.mkdir(parents=True, exist_ok=True)
    for f in (SC / "_background_noise_").glob("*.wav"):
        write_wav(NOISE / f.name, read_wav(f))
    t = np.arange(60 * SR) / SR
    for k, f0 in enumerate([50, 100, 150]):  # motor / transformer hum + PWM whine
        hum = sum(np.sin(2 * np.pi * f0 * h * t + rng.uniform(0, 6)) / h for h in range(1, 8))
        whine = 0.2 * np.sin(2 * np.pi * (2000 + 500 * k) * t)
        x = hum + whine + 0.3 * rng.standard_normal(len(t))
        write_wav(NOISE / f"motor_{f0}hz.wav", normalise(x, 0.3))
    # brown noise (fans / air)
    b = np.cumsum(rng.standard_normal(60 * SR))
    b = b - np.convolve(b, np.ones(4000) / 4000, mode="same")
    write_wav(NOISE / "brown.wav", normalise(b, 0.3))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--positives", type=int, default=4000)
    ap.add_argument("--tts-negatives", type=int, default=3000)
    ap.add_argument("--sc-per-word", type=int, default=600)
    ap.add_argument("--only", choices=["tts", "sc", "noise", "extra"])
    ap.add_argument("--extra-per-voice", type=int, default=300)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    a = ap.parse_args()
    rng = np.random.default_rng(1234 + a.shard)
    random.seed(1234 + a.shard)
    if a.only in (None, "noise"):
        gen_noise(rng)
    if a.only in (None, "sc"):
        gen_sc(a, rng)
    if a.only in (None, "tts"):
        gen_tts(a, rng)
    if a.only in (None, "extra"):
        gen_extra(a, rng)
    for s in ["train", "val", "test"]:
        for lab in ["wake", "unknown"]:
            d = OUT / s / lab
            print(s, lab, len(list(d.glob("*.wav"))) if d.exists() else 0)
