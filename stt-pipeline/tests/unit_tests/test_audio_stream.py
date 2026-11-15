"""Audio stream test: microphone hardware probe, live RMS level check, and queue unit tests."""
import math
import os
import queue
import struct
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from audio_stream import AudioStream, AudioPreprocessor
from config import BYTES_PER_FRAME, CHANNELS, FRAME_MS, SAMPLE_RATE


def calculate_rms(chunk: bytes) -> float:
    """Calculate Root Mean Square (RMS) amplitude of a 16-bit PCM chunk."""
    count = len(chunk) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"<{count}h", chunk)
    sum_squares = sum(s * s for s in shorts)
    return math.sqrt(sum_squares / count)


def run_mic_test() -> None:
    """Run interactive microphone hardware test displaying live RMS level and volume bars."""
    import sounddevice as sd

    print("=" * 70)
    print("AUDIO STREAM - MICROPHONE HARDWARE TEST")
    print(f"Target Specs: {SAMPLE_RATE} Hz | {CHANNELS} Channel (Mono) | {FRAME_MS} ms/frame ({BYTES_PER_FRAME} bytes)")
    print("=" * 70)

    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0]
        print(f"[audio_stream] Default Audio Input Device: [{default_in}] {devices[default_in]['name']}\n")
    except Exception as exc:
        print(f"[audio_stream] Note: Could not query device names ({exc}). Using system default.\n")

    stream = AudioStream()
    stream.start()
    print("Listening to microphone... Speak to test input volume (Ctrl+C to stop):\n")

    try:
        while True:
            try:
                chunk = stream.read_chunk(timeout=1.0)
            except Exception:
                continue

            rms = calculate_rms(chunk)
            # Render a 30-character ASCII meter
            meter_len = min(30, int(rms / 40.0))
            bar = "#" * meter_len + "-" * (30 - meter_len)

            if rms < 30.0:
                level_str = "Quiet/Low"
            elif rms < 300.0:
                level_str = "Normal"
            else:
                level_str = "Loud"

            sys.stdout.write(f"\r  Mic Level: [{bar}] RMS: {rms:6.1f} ({level_str})   ")
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n\n[audio_stream] Mic test stopped.")
    finally:
        stream.stop()


def test_audio_stream_initialization():
    """Unit test verifying default stream parameter specifications."""
    stream = AudioStream()
    assert stream.sample_rate == SAMPLE_RATE
    assert stream.block_size == int(SAMPLE_RATE * (FRAME_MS / 1000.0))
    assert stream._queue.maxsize == 200


def test_audio_stream_queue_overflow_protection():
    """Unit test verifying that queue discards the oldest frame when full without blocking."""
    stream = AudioStream()
    dummy_frame = b"\x00" * BYTES_PER_FRAME

    # Fill queue to capacity (200)
    for i in range(200):
        tagged_frame = i.to_bytes(2, "little") + b"\x00" * (BYTES_PER_FRAME - 2)
        stream._callback(tagged_frame, 320, None, None)

    assert stream._queue.qsize() == 200

    # Insert a 201st frame; should drop frame 0 and keep size at 200
    new_frame = (999).to_bytes(2, "little") + b"\x00" * (BYTES_PER_FRAME - 2)
    stream._callback(new_frame, 320, None, None)

    assert stream._queue.qsize() == 200
    first_frame = stream.read_chunk(timeout=0.1)
    first_tag = int.from_bytes(first_frame[:2], "little")
    # Tag 0 was discarded; first item should be tag 1
    assert first_tag == 1


def test_bandpass_filter_hum_rejection():
    """Unit test verifying that AudioPreprocessor rejects >95% of 60 Hz hum while preserving voiceband."""
    pp = AudioPreprocessor()
    n = int(SAMPLE_RATE * 0.5)
    samples_60 = [
        int(max(-32768, min(32767, 10000.0 * math.sin(2.0 * math.pi * 60.0 * i / SAMPLE_RATE))))
        for i in range(n)
    ]
    hum_60 = struct.pack(f"<{n}h", *samples_60)

    filtered_tail = b""
    for i in range(0, len(hum_60), BYTES_PER_FRAME):
        out = pp.process(hum_60[i : i + BYTES_PER_FRAME])
        if i >= len(hum_60) // 2:
            filtered_tail += out

    tail_samples = struct.unpack(f"<{len(filtered_tail)//2}h", filtered_tail)
    hum_rms = math.sqrt(sum(s * s for s in tail_samples) / len(tail_samples))
    orig_rms = 10000.0 / math.sqrt(2.0)
    assert hum_rms < 0.05 * orig_rms, f"Expected >95% 60Hz hum rejection, got remaining ratio={hum_rms/orig_rms:.3f}"

    pp.reset()
    samples_1k = [
        int(max(-32768, min(32767, 5000.0 * math.sin(2.0 * math.pi * 1000.0 * i / SAMPLE_RATE))))
        for i in range(n)
    ]
    voice_1khz = struct.pack(f"<{n}h", *samples_1k)
    voice_tail = b""
    for i in range(0, len(voice_1khz), BYTES_PER_FRAME):
        out = pp.process(voice_1khz[i : i + BYTES_PER_FRAME])
        if i >= len(voice_1khz) // 2:
            voice_tail += out

    v_samples = struct.unpack(f"<{len(voice_tail)//2}h", voice_tail)
    voice_rms = math.sqrt(sum(s * s for s in v_samples) / len(v_samples))
    orig_v_rms = 5000.0 / math.sqrt(2.0)
    assert voice_rms > 0.90 * orig_v_rms, f"Expected voiceband pass-through >90%, got {voice_rms/orig_v_rms:.3f}"


if __name__ == "__main__":
    run_mic_test()
