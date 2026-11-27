"""In-memory live state of the station, fed by the orchestrator ingest loop."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from .config import Settings
from .hub import WebSocketHub
from .schemas import (
    CameraStatus,
    ConnectionStatus,
    DiagnosticsSnapshot,
    FrameMeta,
    HmiEvent,
    HmiSettings,
    InspectionResult,
    StateChange,
    StateSnapshot,
    StationInfo,
    TelemetryPoint,
    TelemetrySnapshot,
    VoiceEntry,
    VoiceStatus,
)


@dataclass(slots=True)
class Frame:
    jpeg: bytes
    meta: FrameMeta
    received_at: float = field(default_factory=time.time)


class FrameStore:
    """Latest camera frame plus waiters for the MJPEG stream."""

    def __init__(self) -> None:
        self._frame: Frame | None = None
        self._version = 0
        self._cond = asyncio.Condition()

    @property
    def latest(self) -> Frame | None:
        return self._frame

    @property
    def version(self) -> int:
        return self._version

    async def publish(self, frame: Frame) -> None:
        async with self._cond:
            self._frame = frame
            self._version += 1
            self._cond.notify_all()

    async def wait_newer(self, version: int, timeout: float) -> Frame | None:
        async with self._cond:
            if self._version == version:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=timeout)
                except TimeoutError:
                    return None
            return self._frame


class SnapshotCache:
    """Bounded LRU of evidence JPEGs keyed by event id."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._items: OrderedDict[str, bytes] = OrderedDict()

    def put(self, event_id: str, jpeg: bytes) -> None:
        self._items[event_id] = jpeg
        self._items.move_to_end(event_id)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def get(self, event_id: str) -> bytes | None:
        return self._items.get(event_id)

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._items


class HmiState:
    def __init__(self, settings: Settings, hub: WebSocketHub) -> None:
        self._settings = settings
        self._hub = hub
        self.station = StationInfo(
            id=settings.station_id, name=settings.station_name, host=settings.station_host
        )
        self.fsm: StateChange | None = None
        self.telemetry: TelemetrySnapshot | None = None
        self.history: deque[TelemetryPoint] = deque(maxlen=settings.history_points)
        self.events: deque[HmiEvent] = deque(maxlen=settings.recent_events)
        self.inspection: InspectionResult | None = None
        self.voice_status: VoiceStatus | None = None
        self.voice: deque[VoiceEntry] = deque(maxlen=settings.voice_entries)
        self.diagnostics: DiagnosticsSnapshot | None = None
        self.frames = FrameStore()
        self.snapshots = SnapshotCache(settings.snapshot_cache)
        self.connection = ConnectionStatus()
        self.hmi_settings = HmiSettings()
        self._last_history_ts = 0.0

    # ------------------------------------------------------------------
    # Mutators (each one also broadcasts to WebSocket clients)
    # ------------------------------------------------------------------

    def touch(self) -> None:
        self.connection.last_message_at = time.time()

    def set_telemetry(self, snap: TelemetrySnapshot) -> None:
        self.telemetry = snap
        # Down-sample sparkline history to <= 2 points / s.
        if snap.ts - self._last_history_ts >= 0.5:
            self._last_history_ts = snap.ts
            self.history.append(
                TelemetryPoint(
                    ts=snap.ts,
                    speed=snap.belt_speed_mms,
                    current=snap.motor_current_a,
                    temp=snap.motor_temp_c,
                )
            )
        self._hub.broadcast("telemetry", snap)

    def set_state(self, change: StateChange) -> None:
        self.fsm = change
        self._hub.broadcast("state", change)

    def add_event(self, event: HmiEvent) -> None:
        if event.station_id is None:
            event = event.model_copy(update={"station_id": self.station.id})
        if event.snapshot and event.event_id not in self.snapshots:
            frame = self.frames.latest
            if frame is not None:
                self.snapshots.put(event.event_id, frame.jpeg)
        self.events.append(event)
        self._hub.broadcast("event", event)

    def set_inspection(self, result: InspectionResult) -> None:
        self.inspection = result
        self._hub.broadcast("inspection", result)

    def add_voice(self, entry: VoiceEntry) -> None:
        self.voice.append(entry)
        self._hub.broadcast("voice", entry)

    def set_voice_status(self, status: VoiceStatus) -> None:
        self.voice_status = status
        self._hub.broadcast("voice_status", status)

    def set_diagnostics(self, diag: DiagnosticsSnapshot) -> None:
        self.diagnostics = diag
        self._hub.broadcast("diagnostics", diag)

    async def set_frame(self, frame: Frame) -> None:
        first = self.frames.latest is None
        await self.frames.publish(frame)
        if first:
            self.notify_camera()

    def notify_camera(self) -> None:
        self._hub.broadcast("camera", self.camera_status())

    def put_snapshot(self, event_id: str, jpeg: bytes) -> None:
        self.snapshots.put(event_id, jpeg)
        self._hub.broadcast("snapshot_ready", {"event_id": event_id})

    def set_settings(self, settings: HmiSettings) -> None:
        self.hmi_settings = settings
        self._hub.broadcast("settings", settings)

    def set_connection(self, **changes: object) -> None:
        updated = self.connection.model_copy(update=changes)
        if updated.model_dump(exclude={"last_message_at"}) != self.connection.model_dump(
            exclude={"last_message_at"}
        ):
            self.connection = updated
            self._hub.broadcast("connection", self.connection)
        else:
            self.connection = updated

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def camera_status(self) -> CameraStatus:
        frame = self.frames.latest
        if frame is None:
            return CameraStatus()
        online = (time.time() - frame.received_at) <= self._settings.stale_after_s
        return CameraStatus(
            online=online,
            camera_id=frame.meta.camera_id,
            width=frame.meta.width,
            height=frame.meta.height,
            fps=frame.meta.fps,
            latency_ms=frame.meta.latency_ms,
            led_on=frame.meta.led_on,
            last_frame_at=frame.received_at,
        )

    def snapshot(self) -> StateSnapshot:
        return StateSnapshot(
            station=self.station,
            fsm=self.fsm,
            telemetry=self.telemetry,
            history=list(self.history),
            events=list(self.events)[-50:],
            inspection=self.inspection,
            voice_status=self.voice_status,
            voice=list(self.voice),
            diagnostics=self.diagnostics,
            camera=self.camera_status(),
            connection=self.connection,
            settings=self.hmi_settings,
        )
