"""ZeroMQ PUSH socket client for transmitting structured voice events."""
import os
import zmq  # type: ignore

from config import ZMQ_ENDPOINT


class ZMQPusher:
    """Publishes voice pipeline events over a ZeroMQ PUSH socket."""

    def __init__(self, endpoint: str = ZMQ_ENDPOINT) -> None:
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PUSH)

        # Set send queue limit to avoid unbounded memory growth
        self._socket.setsockopt(zmq.SNDHWM, 50)
        # Discard unsent messages immediately upon socket close
        self._socket.setsockopt(zmq.LINGER, 0)

        # Create target directory for UNIX domain sockets if needed
        if endpoint.startswith("ipc://"):
            ipc_path = endpoint[len("ipc://"):]
            os.makedirs(os.path.dirname(ipc_path), exist_ok=True)

        self._socket.connect(endpoint)
        self.endpoint = endpoint
        print(f"[zmq_client] Connected to {endpoint}")

    def publish_event(self, event) -> None:
        """Serialize a Pydantic event object to JSON and send it over the socket."""
        self._socket.send_json(event.model_dump())

    def close(self) -> None:
        """Close the socket and terminate the ZeroMQ context."""
        self._socket.close()
        self._context.term()
        print("[zmq_client] Socket closed.")
