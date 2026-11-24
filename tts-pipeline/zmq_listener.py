"""ZeroMQ PULL listener and PUB status emitter for the TTS microservice."""
import json
import os
from typing import Optional

import zmq

try:
    from config import ZMQ_PUB_ENDPOINT, ZMQ_PULL_ENDPOINT
    from schemas import TTSRequest, TTSStatusEvent
except ImportError:
    from .config import ZMQ_PUB_ENDPOINT, ZMQ_PULL_ENDPOINT
    from .schemas import TTSRequest, TTSStatusEvent


class ZMQListener:
    """Receives TTSRequest commands over a ZeroMQ PULL socket and publishes status events."""

    def __init__(
        self,
        pull_endpoint: str = ZMQ_PULL_ENDPOINT,
        pub_endpoint: Optional[str] = ZMQ_PUB_ENDPOINT,
        bind_pull: bool = True,
    ) -> None:
        self.pull_endpoint = pull_endpoint
        self.pub_endpoint = pub_endpoint
        self._context = zmq.Context()

        # 1. PULL socket for incoming TTS requests
        self._pull_socket = self._context.socket(zmq.PULL)
        self._pull_socket.setsockopt(zmq.RCVHWM, 50)
        self._pull_socket.setsockopt(zmq.LINGER, 0)

        # Handle UNIX domain socket path creation if necessary
        if pull_endpoint.startswith("ipc://"):
            ipc_path = pull_endpoint[len("ipc://"):]
            os.makedirs(os.path.dirname(ipc_path), exist_ok=True)

        if bind_pull:
            self._pull_socket.bind(pull_endpoint)
            print(f"[zmq_listener] Bound PULL socket to {pull_endpoint}")
        else:
            self._pull_socket.connect(pull_endpoint)
            print(f"[zmq_listener] Connected PULL socket to {pull_endpoint}")

        # 2. Optional PUB socket for status feedback
        self._pub_socket = None
        if self.pub_endpoint:
            self._pub_socket = self._context.socket(zmq.PUB)
            self._pub_socket.setsockopt(zmq.SNDHWM, 50)
            self._pub_socket.setsockopt(zmq.LINGER, 0)

            if self.pub_endpoint.startswith("ipc://"):
                pub_ipc = self.pub_endpoint[len("ipc://"):]
                os.makedirs(os.path.dirname(pub_ipc), exist_ok=True)

            try:
                self._pub_socket.bind(self.pub_endpoint)
                print(f"[zmq_listener] Bound status PUB socket to {self.pub_endpoint}")
            except Exception as err:
                print(f"[zmq_listener] Warning: Failed to bind PUB socket ({err})")
                self._pub_socket = None

        self._poller = zmq.Poller()
        self._poller.register(self._pull_socket, zmq.POLLIN)

    def recv_request(self, timeout_ms: int = 500) -> Optional[TTSRequest]:
        """Poll and receive an incoming TTSRequest payload."""
        socks = dict(self._poller.poll(timeout_ms))
        if self._pull_socket in socks and socks[self._pull_socket] == zmq.POLLIN:
            try:
                raw_data = self._pull_socket.recv_json()
                return TTSRequest.model_validate(raw_data)
            except Exception as err:
                print(f"[zmq_listener] Malformed request rejected: {err}")
                return None
        return None

    def publish_status(self, event: TTSStatusEvent) -> None:
        """Broadcast a status event over ZeroMQ PUB socket."""
        if self._pub_socket is not None:
            try:
                self._pub_socket.send_json(event.model_dump())
            except Exception as err:
                print(f"[zmq_listener] Failed to publish status: {err}")

    def close(self) -> None:
        """Gracefully close all sockets and terminate context."""
        try:
            self._pull_socket.close()
            if self._pub_socket:
                self._pub_socket.close()
            self._context.term()
            print("[zmq_listener] Sockets closed cleanly.")
        except Exception:
            pass
