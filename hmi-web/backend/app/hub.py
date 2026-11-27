"""WebSocket fan-out hub.

Each connected client gets a bounded queue drained by its own sender task, so a
slow browser tab can never stall ingestion.  When a client queue is full the
oldest message is dropped (telemetry is superseded by newer frames anyway).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel

from .schemas import WsMessage

LOGGER = logging.getLogger("eaa.hmi.hub")


def _encode(message: WsMessage) -> str:
    return message.model_dump_json()


class _Client:
    __slots__ = ("socket", "queue", "task")

    def __init__(self, socket: WebSocket, queue_size: int) -> None:
        self.socket = socket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.task: asyncio.Task[None] | None = None

    def offer(self, text: str) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(text)


class WebSocketHub:
    def __init__(self, queue_size: int = 512) -> None:
        self._queue_size = queue_size
        self._clients: set[_Client] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def serve(self, socket: WebSocket, initial: WsMessage) -> None:
        """Accept *socket*, send *initial*, then stream broadcasts until disconnect."""
        await socket.accept()
        client = _Client(socket, self._queue_size)
        client.offer(_encode(initial))
        self._clients.add(client)
        client.task = asyncio.create_task(self._sender(client), name="hmi-ws-sender")
        LOGGER.info("WebSocket client connected (%d active)", len(self._clients))
        try:
            while True:
                # Client -> server traffic is limited to keepalive pings.
                text = await socket.receive_text()
                if text == "ping":
                    client.offer(json.dumps({"type": "pong", "data": None}))
        except Exception:
            pass
        finally:
            self._clients.discard(client)
            if client.task is not None:
                client.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await client.task
            LOGGER.info("WebSocket client disconnected (%d active)", len(self._clients))

    async def _sender(self, client: _Client) -> None:
        try:
            while True:
                text = await client.queue.get()
                await client.socket.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("WebSocket send failed; closing client", exc_info=True)
            with contextlib.suppress(Exception):
                await client.socket.close()

    def broadcast(self, msg_type: str, data: Any) -> None:
        if not self._clients:
            return
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        text = _encode(WsMessage(type=msg_type, data=data))
        for client in tuple(self._clients):
            client.offer(text)

    async def close(self) -> None:
        for client in tuple(self._clients):
            with contextlib.suppress(Exception):
                await client.socket.close()
            if client.task is not None:
                client.task.cancel()
        self._clients.clear()
