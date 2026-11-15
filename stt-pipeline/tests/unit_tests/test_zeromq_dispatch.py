"""ZeroMQ dispatch test: listens for live JSON events emitted by the STT voice pipeline."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import zmq  # type: ignore
from config import ZMQ_ENDPOINT
from schemas import VoiceIntentEvent, VoiceStopEvent
from zmq_client import ZMQPusher


def run_listener(endpoint: str = ZMQ_ENDPOINT) -> None:
    """Run interactive ZeroMQ PULL listener to monitor live events from the pipeline."""
    context = zmq.Context()
    receiver = context.socket(zmq.PULL)
    try:
        receiver.bind(endpoint)
    except zmq.error.ZMQError as exc:
        print(f"[test_zeromq_dispatch] Could not bind to {endpoint}: {exc}")
        print("  Hint: An existing receiver process is already listening on this port.")
        receiver.close()
        context.term()
        return

    print("=" * 70)
    print("ZEROMQ DISPATCH LISTENER")
    print(f"Listening on {endpoint}...")
    print("Waiting for events from 'python main.py' (Ctrl+C to stop)...")
    print("=" * 70 + "\n")

    try:
        while True:
            data: dict = receiver.recv_json()
            event_type = data.get("event_type", "UNKNOWN")
            seq = data.get("seq", 0)
            conf = data.get("confidence", 0.0)

            if event_type == "VOICE_STOP_DETECTED":
                print(f"[EVENT] >>> SAFETY STOP DETECTED (seq={seq}) <<<")
                print(f"        Confidence : {conf * 100:.1f}%")
                print(f"        Timestamp  : {data.get('timestamp')}")
                print(f"        Raw JSON   : {json.dumps(data)}\n")
            elif event_type == "VOICE_INTENT":
                print(f"[EVENT] --- COMMAND INTENT (seq={seq}) ---")
                print(f"        Intent     : {data.get('intent')}")
                print(f"        Transcript : \"{data.get('raw_transcript')}\"")
                print(f"        Confidence : {conf * 100:.1f}%")
                print(f"        Latency    : {data.get('latency_ms', 0.0):.1f} ms")
                print(f"        Parameters : {data.get('parameters')}")
                print(f"        Raw JSON   : {json.dumps(data)}\n")
            elif event_type == "VOICE_WAKE_DETECTED":
                print(f"[EVENT] ... WAKE WORD DETECTED (seq={seq}) ...")
                print(f"        Phrase     : '{data.get('wake_phrase')}'")
                print(f"        Confidence : {conf * 100:.1f}%\n")
            else:
                print(f"[EVENT] Generic: {json.dumps(data)}\n")

    except KeyboardInterrupt:
        print("\n[test_zeromq_dispatch] Listener stopped.")
    finally:
        receiver.close()
        context.term()


def test_zeromq_pipeline_dispatch():
    """Unit test verifying that ZMQPusher publishes conforming JSON events to a PULL receiver."""
    test_endpoint = "tcp://127.0.0.1:15577"
    ctx = zmq.Context()
    pull = ctx.socket(zmq.PULL)
    pull.bind(test_endpoint)

    try:
        pusher = ZMQPusher(endpoint=test_endpoint)

        stop_ev = VoiceStopEvent(seq=1, confidence=0.98)
        pusher.publish_event(stop_ev)

        intent_ev = VoiceIntentEvent(
            seq=2,
            raw_transcript="start conveyor",
            intent="CMD_START",
            confidence=0.95,
            latency_ms=32.1,
            parameters={},
        )
        pusher.publish_event(intent_ev)

        # Receive and assert payloads
        ev1 = pull.recv_json(flags=0)
        assert ev1["event_type"] == "VOICE_STOP_DETECTED"
        assert ev1["seq"] == 1
        assert ev1["confidence"] == 0.98

        ev2 = pull.recv_json(flags=0)
        assert ev2["event_type"] == "VOICE_INTENT"
        assert ev2["intent"] == "CMD_START"
        assert ev2["raw_transcript"] == "start conveyor"
        assert ev2["seq"] == 2

        pusher.close()
    finally:
        pull.close()
        ctx.term()


if __name__ == "__main__":
    run_listener()
