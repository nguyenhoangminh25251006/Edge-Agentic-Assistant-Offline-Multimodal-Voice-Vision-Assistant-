"""Voice pipeline coordinator.

Captures spoken supervisory commands using Voice Activity Detection (VAD),
transcribes speech via Vosk STT with constrained grammar, parses canonical intents,
and emits structured events over ZeroMQ.

Note: Continuous Wake Word (KWS) detection has been offloaded to the STM32 MCU firmware.
This microservice focuses on direct on-demand command speech-to-text.
"""
import math
import os
import struct
import sys
import time

from audio_stream import AudioStream
from audio_buffer_vad import VADSession
from config import (
    VOSK_MODEL_PATH,
    get_vosk_model_path,
)
from intent_parser import IntentParser
from schemas import VoiceIntentEvent, VoiceStopEvent
from stt_engine import VoskSTTEngine
from zmq_client import ZMQPusher


def build_stt_engine() -> VoskSTTEngine:
    """Instantiate the speech-to-text engine based on configuration."""
    model_path = get_vosk_model_path()

    if not model_path or not os.path.isdir(model_path):
        raise FileNotFoundError(
            f"Vosk model directory not found: '{model_path}'. Check VOSK_MODEL_PATH."
        )

    print(f"[main] STT Engine: VoskSTTEngine (model: '{model_path}')")
    return VoskSTTEngine(
        model_path=model_path,
        grammar=IntentParser.all_grammar_phrases(),
    )


def calculate_rms(chunk: bytes) -> float:
    """Calculate Root Mean Square (RMS) amplitude of a 16-bit PCM chunk."""
    count = len(chunk) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"<{count}h", chunk)
    sum_squares = sum(s * s for s in shorts)
    return math.sqrt(sum_squares / count)


def main() -> None:
    audio_file = None
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        audio_file = sys.argv[1]
    elif "--audio" in sys.argv:
        idx = sys.argv.index("--audio")
        if idx + 1 < len(sys.argv):
            audio_file = sys.argv[idx + 1]
    audio_file = audio_file or os.getenv("AUDIO_FILE")

    print("=" * 70)
    print("EDGE AGENTIC ASSISTANT - STT VOICE PIPELINE")
    print("Target Edge: Arduino UNO Q (Qualcomm QRB2210 + STM32U585)")
    print("Engine Mode: PRODUCTION (Offline Vosk Kaldi)")
    if audio_file:
        print(f"Audio Input: Simulated File ('{audio_file}')")
    else:
        print("Audio Input: Live Microphone")
    print("=" * 70)

    audio_stream = AudioStream(audio_file=audio_file)
    pusher = ZMQPusher()
    stt_engine = build_stt_engine()
    vad = VADSession()

    seq: int = 0
    audio_stream.start()
    vad.start()

    print(f"[main] ZeroMQ Egress: {pusher.endpoint}")
    if audio_file:
        print(f"[main] Ingesting simulated audio stream from: {audio_file}\n")
    else:
        print("[main] Listening for spoken supervisory commands (Ctrl+C to stop)...")
        print("       Examples: 'start conveyor', 'stop', 'speed up', 'set speed fast', 'system status'\n")

    was_speaking = False
    last_heartbeat = time.monotonic()
    max_rms_seen = 0.0

    try:
        while True:
            try:
                chunk = audio_stream.read_chunk(timeout=0.5)
            except EOFError:
                print("[main] Audio stream completed.")
                break
            except Exception:
                if audio_stream.is_file_mode and audio_stream.is_exhausted:
                    print("[main] Audio stream completed.")
                    break
                continue

            chunk_rms = calculate_rms(chunk)
            if chunk_rms > max_rms_seen:
                max_rms_seen = chunk_rms

            now = time.monotonic()
            if now - last_heartbeat >= 2.5:
                if not vad._speaking:
                    if max_rms_seen < 30.0:
                        level_note = "Quiet/Low - check mic volume or speak closer"
                    else:
                        level_note = "Good"
                    print(f"  [Mic Active] RMS={max_rms_seen:.1f} ({level_note}) | Waiting for voice command...")
                max_rms_seen = 0.0
                last_heartbeat = now

            speech_bytes = vad.feed(chunk)

            if vad._speaking and not was_speaking:
                print("  --> [Speech detected! Ingesting utterance...]")
                was_speaking = True

            if speech_bytes is not None:
                was_speaking = False
                t0 = time.perf_counter()
                result = stt_engine.transcribe(speech_bytes)
                latency_ms = (time.perf_counter() - t0) * 1000.0

                text = result["text"]
                conf = result["confidence"]
                audio_sec = len(speech_bytes) / (16000 * 2)

                if text:
                    seq += 1
                    intent, params = IntentParser.parse(text)
                    is_safety = intent in ("CMD_STOP", "CMD_ESTOP")

                    print("\n" + "=" * 70)
                    if is_safety:
                        print("  [SAFETY EVENT DETECTED - CONVEYOR HALT]")
                    else:
                        print("  [VOICE COMMAND DETECTED]")
                    print(f"  Transcript : \"{text}\"")
                    print(f"  Confidence : {conf * 100:.1f}%")
                    print(f"  Latency    : {latency_ms:.1f} ms")
                    print(f"  Audio Len  : {audio_sec:.2f} s ({len(speech_bytes):,} bytes)")
                    print(f"  Intent     : {intent}")
                    print(f"  Parameters : {params}")

                    # Immediate emergency stop handling
                    if is_safety:
                        stop_event = VoiceStopEvent(
                            seq=seq,
                            confidence=conf,
                        )
                        pusher.publish_event(stop_event)
                        print(f"  ZeroMQ     : Dispatched VOICE_STOP_DETECTED + VOICE_INTENT (seq={seq})")
                    else:
                        print(f"  ZeroMQ     : Dispatched VOICE_INTENT (seq={seq})")

                    intent_event = VoiceIntentEvent(
                        seq=seq,
                        raw_transcript=text,
                        intent=intent,
                        confidence=conf,
                        latency_ms=round(latency_ms, 2),
                        parameters=params,
                    )
                    pusher.publish_event(intent_event)
                    print("=" * 70 + "\n")
                else:
                    print("  [Unrecognized / Background Chatter] Filtered safely (Confidence: 0.0%)\n")

                vad.start()

            elif not vad.active:
                was_speaking = False
                vad.start()

    except KeyboardInterrupt:
        print("\n[main] Shutting down...")
    finally:
        audio_stream.stop()
        pusher.close()
        print("[main] Stopped.")


if __name__ == "__main__":
    main()
