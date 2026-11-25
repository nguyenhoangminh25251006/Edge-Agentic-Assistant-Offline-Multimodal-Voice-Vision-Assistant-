"""Receive command audio from the Pico 2 audio channel (USB CDC ACM 1) and save WAV files.

    python3 audio_rx.py --port /dev/ttyACM1 --out captures/

Frame format (firmware src/voice/audio_out.c):
    A5 5A | type u8 | seq u16 | len u16 | payload | crc16 CCITT-FALSE over type..payload
    type 1 START (event_seq u16, rate u16, bits u8, channels u8), 2 DATA (int16 LE), 3 END (total u32)
Each START..END becomes one 16 kHz / 16-bit mono WAV, the exact input format of the
team's Vosk STT pipeline (branch stt-tts, stt-pipeline/stt_engine/stt_adapter.py).
`AudioReceiver` is also used by voice_to_belt.py.
"""
from __future__ import annotations

import argparse
import os
import struct
import time
import wave
from typing import Callable, Optional

import serial

from b03 import crc16


class AudioReceiver:
    def __init__(self, port: str, on_utterance: Callable[[bytes, int], None]) -> None:
        # Baud rate is ignored by USB CDC; it matters only on a real UART (UNO Q: >= 921600).
        self.ser = serial.Serial(port, 921600, timeout=0.05)
        self.on_utterance = on_utterance
        self.buf = bytearray()
        self.pcm: Optional[bytearray] = None
        self.rate = 16000
        self.bad = 0

    def poll(self) -> None:
        self.buf += self.ser.read(4096)
        while True:
            i = self.buf.find(b"\xA5\x5A")
            if i < 0:
                del self.buf[:-1]
                return
            del self.buf[:i]
            if len(self.buf) < 7:
                return
            t, seq, n = struct.unpack_from("<BHH", self.buf, 2)
            if n > 4096:
                del self.buf[:2]
                self.bad += 1
                continue
            if len(self.buf) < 7 + n + 2:
                return
            body = bytes(self.buf[2:7 + n])
            crc = struct.unpack_from("<H", self.buf, 7 + n)[0]
            del self.buf[:7 + n + 2]
            if crc16(body) != crc:
                self.bad += 1
                continue
            payload = body[5:]
            if t == 1:
                _, self.rate, _, _ = struct.unpack("<HHBB", payload)
                self.pcm = bytearray()
            elif t == 2 and self.pcm is not None:
                self.pcm += payload
            elif t == 3 and self.pcm is not None:
                self.on_utterance(bytes(self.pcm), self.rate)
                self.pcm = None


def save_wav(path: str, pcm: bytes, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM1")
    ap.add_argument("--out", default="captures")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    def done(pcm: bytes, rate: int) -> None:
        name = os.path.join(a.out, time.strftime("cmd_%Y%m%d_%H%M%S.wav"))
        save_wav(name, pcm, rate)
        print(f"saved {name}: {len(pcm) / 2 / rate:.2f} s")

    rx = AudioReceiver(a.port, done)
    print(f"listening on {a.port} (press the push-to-talk button or say the wake word)")
    while True:
        rx.poll()
