"""Unit tests for ZeroMQ listener and status publishing."""
import os
import sys
import time
import pytest
import zmq

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent
from zmq_listener import ZMQListener


def test_zmq_listener_request_dispatch():
    test_pull_port = "tcp://127.0.0.1:55991"
    test_pub_port = "tcp://127.0.0.1:55992"

    listener = ZMQListener(
        pull_endpoint=test_pull_port,
        pub_endpoint=test_pub_port,
        bind_pull=True,
    )

    ctx = zmq.Context()

    # 1. Test PUSH client sending TTSRequest to listener
    pusher = ctx.socket(zmq.PUSH)
    pusher.connect(test_pull_port)

    # 2. Test SUB client subscribing to status events
    subscriber = ctx.socket(zmq.SUB)
    subscriber.connect(test_pub_port)
    subscriber.setsockopt_string(zmq.SUBSCRIBE, "")

    time.sleep(0.1)  # Allow ZeroMQ handshakes

    # Send a request
    sample_req = TTSRequest(
        request_id="zmq_test_1",
        text="Photoelectric sensor clear",
        priority=PriorityEnum.NORMAL,
    )
    pusher.send_json(sample_req.model_dump())

    # Listener receives request
    received = listener.recv_request(timeout_ms=1000)
    assert received is not None
    assert received.request_id == "zmq_test_1"
    assert received.text == "Photoelectric sensor clear"
    assert received.priority == PriorityEnum.NORMAL

    # Test status publish
    sample_evt = TTSStatusEvent(
        request_id="zmq_test_1",
        status=TTSStatusEnum.COMPLETED,
        cached=False,
        latency_ms=15.0,
        duration_s=1.2,
    )
    listener.publish_status(sample_evt)

    # Subscriber receives event
    socks = dict(zmq.Poller().register(subscriber, zmq.POLLIN) or {})
    # Simple poll
    poller = zmq.Poller()
    poller.register(subscriber, zmq.POLLIN)
    events = dict(poller.poll(1000))
    assert subscriber in events

    status_data = subscriber.recv_json()
    assert status_data["request_id"] == "zmq_test_1"
    assert status_data["status"] == "COMPLETED"

    # Cleanup
    pusher.close()
    subscriber.close()
    ctx.term()
    listener.close()
