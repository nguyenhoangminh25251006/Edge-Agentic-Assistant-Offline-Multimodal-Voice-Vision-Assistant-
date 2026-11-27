"""Operator command egress: HMI -> orchestrator ``ui.pull`` (``UICommandWire``).

Safety notes ("the MPU proposes, the MCU decides"):

* The HMI only *proposes* commands.  The orchestrator FSM, the Safety Arbiter and
  ultimately the MCU firmware decide whether anything moves.
* ``ZMQ_IMMEDIATE`` is set so commands are never queued for a peer that is not
  connected; a stale "start" must not be delivered minutes later.  When the
  orchestrator is unreachable the send fails fast and the API returns 503.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import zmq
import zmq.asyncio

from .config import Settings

LOGGER = logging.getLogger("eaa.hmi.commands")


class CommandChannelUnavailable(RuntimeError):
    pass


class CommandPublisher:
    def __init__(self, settings: Settings, context: zmq.asyncio.Context) -> None:
        self._settings = settings
        self._context = context
        self._socket: zmq.asyncio.Socket | None = None

    def start(self) -> None:
        endpoint = self._settings.ui_push_endpoint
        if endpoint.startswith("ipc://"):
            os.makedirs(os.path.dirname(endpoint[len("ipc://") :]), exist_ok=True)
        socket = self._context.socket(zmq.PUSH)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.IMMEDIATE, 1)
        socket.setsockopt(zmq.SNDHWM, 16)
        socket.connect(endpoint)
        self._socket = socket
        LOGGER.info("Command channel connected to %s", endpoint)

    def stop(self) -> None:
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None

    async def send(self, action: str, parameters: dict[str, Any] | None = None) -> str:
        """Send a ``UICommandWire`` message; returns the generated command id."""
        if self._socket is None:
            raise CommandChannelUnavailable("Command channel is not started")
        command_id = uuid.uuid4().hex
        flat: dict[str, Any] = {
            k: v for k, v in (parameters or {}).items() if isinstance(v, str | int | float | bool)
        }
        flat.update(
            {
                "command_id": command_id,
                "origin": "hmi",
                "station_id": self._settings.station_id,
                "operator": self._settings.operator_name,
                "issued_at": time.time(),
            }
        )
        message = {"action": action, "parameters": flat}
        try:
            await self._socket.send_json(message, flags=zmq.NOBLOCK)
        except zmq.Again as exc:
            raise CommandChannelUnavailable(
                "Orchestrator command channel is not reachable"
            ) from exc
        LOGGER.info("Command sent: %s %s", action, flat)
        return command_id
