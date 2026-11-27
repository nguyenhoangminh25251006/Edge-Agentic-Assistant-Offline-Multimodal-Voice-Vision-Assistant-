"""ZeroMQ ingest: orchestrator HMI PUB socket -> HmiState -> WebSocket clients.

Wire format (multipart messages on the ``hmi.pub`` PUB socket):

=====================  ==========================================================
Topic (part 1)          Remaining parts
=====================  ==========================================================
``hmi.telemetry``       JSON :class:`TelemetrySnapshot`
``hmi.state``           JSON :class:`StateChange`
``hmi.event``           JSON :class:`HmiEvent`
``hmi.inspection``      JSON :class:`InspectionResult`
``hmi.voice``           JSON :class:`VoiceEntry`
``hmi.voice_status``    JSON :class:`VoiceStatus`
``hmi.diagnostics``     JSON :class:`DiagnosticsSnapshot`
``hmi.frame``           JSON :class:`FrameMeta`, JPEG bytes
``hmi.snapshot``        JSON :class:`SnapshotMeta`, JPEG bytes
=====================  ==========================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import zmq
import zmq.asyncio
from pydantic import BaseModel, ValidationError

from .config import Settings
from .schemas import (
    DiagnosticsSnapshot,
    FrameMeta,
    HmiEvent,
    InspectionResult,
    SnapshotMeta,
    StateChange,
    TelemetrySnapshot,
    VoiceEntry,
    VoiceStatus,
)
from .state import Frame, HmiState

LOGGER = logging.getLogger("eaa.hmi.ingest")

MAX_JSON_BYTES = 256 * 1024
MAX_JPEG_BYTES = 4 * 1024 * 1024


class OrchestratorIngest:
    def __init__(self, settings: Settings, state: HmiState, context: zmq.asyncio.Context) -> None:
        self._settings = settings
        self._state = state
        self._context = context
        self._socket: zmq.asyncio.Socket | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.stats: dict[str, int] = {"received": 0, "invalid": 0, "unknown_topic": 0}

        self._json_handlers: dict[str, tuple[type[BaseModel], Callable[[Any], None]]] = {
            "hmi.telemetry": (TelemetrySnapshot, state.set_telemetry),
            "hmi.state": (StateChange, state.set_state),
            "hmi.event": (HmiEvent, state.add_event),
            "hmi.inspection": (InspectionResult, state.set_inspection),
            "hmi.voice": (VoiceEntry, state.add_voice),
            "hmi.voice_status": (VoiceStatus, state.set_voice_status),
            "hmi.diagnostics": (DiagnosticsSnapshot, state.set_diagnostics),
        }

    async def start(self) -> None:
        endpoint = self._settings.hmi_sub_endpoint
        if endpoint.startswith("ipc://"):
            os.makedirs(os.path.dirname(endpoint[len("ipc://") :]), exist_ok=True)
        socket = self._context.socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVHWM, 1000)
        socket.setsockopt(zmq.SUBSCRIBE, b"hmi.")
        socket.connect(endpoint)
        self._socket = socket
        self._tasks = [
            asyncio.create_task(self._recv_loop(), name="hmi-ingest"),
            asyncio.create_task(self._watchdog(), name="hmi-watchdog"),
        ]
        LOGGER.info("Subscribed to orchestrator HMI feed at %s", endpoint)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None

    async def _recv_loop(self) -> None:
        assert self._socket is not None
        while True:
            try:
                parts = await self._socket.recv_multipart()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("ZeroMQ receive failed")
                await asyncio.sleep(0.5)
                continue
            try:
                await self._dispatch(parts)
            except Exception:
                self.stats["invalid"] += 1
                LOGGER.exception("Failed to handle HMI message")

    async def _dispatch(self, parts: list[bytes]) -> None:
        if len(parts) < 2:
            self.stats["invalid"] += 1
            return
        topic = parts[0].decode("utf-8", errors="replace")
        self.stats["received"] += 1
        self._state.touch()

        if topic in ("hmi.frame", "hmi.snapshot"):
            if len(parts) < 3 or len(parts[2]) > MAX_JPEG_BYTES:
                self.stats["invalid"] += 1
                return
            meta_raw = self._load_json(parts[1])
            if meta_raw is None:
                return
            try:
                if topic == "hmi.frame":
                    meta = FrameMeta.model_validate(meta_raw)
                    await self._state.set_frame(Frame(jpeg=parts[2], meta=meta))
                else:
                    snap = SnapshotMeta.model_validate(meta_raw)
                    self._state.put_snapshot(snap.event_id, parts[2])
            except ValidationError as exc:
                self.stats["invalid"] += 1
                LOGGER.warning("Invalid %s metadata: %s", topic, exc.errors(include_url=False))
            return

        handler = self._json_handlers.get(topic)
        if handler is None:
            self.stats["unknown_topic"] += 1
            return
        raw = self._load_json(parts[1])
        if raw is None:
            return
        model, apply = handler
        try:
            apply(model.model_validate(raw))
        except ValidationError as exc:
            self.stats["invalid"] += 1
            LOGGER.warning("Invalid %s payload: %s", topic, exc.errors(include_url=False))

    def _load_json(self, data: bytes) -> Any | None:
        if len(data) > MAX_JSON_BYTES:
            self.stats["invalid"] += 1
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            self.stats["invalid"] += 1
            return None

    async def _watchdog(self) -> None:
        """Track orchestrator liveness and camera staleness once per second."""
        camera_online = False
        while True:
            await asyncio.sleep(1.0)
            last = self._state.connection.last_message_at
            connected = last is not None and (time.time() - last) <= self._settings.stale_after_s
            self._state.set_connection(orchestrator_connected=connected)
            cam = self._state.camera_status()
            if cam.online != camera_online:
                camera_online = cam.online
                self._state.notify_camera()
