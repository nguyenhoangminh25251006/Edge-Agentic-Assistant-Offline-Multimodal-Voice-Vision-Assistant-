"""Pydantic models for the HMI gateway.

Two families of models live here:

1. **Inbound wire contracts** published by ``container-orchestrator`` on the HMI
   PUB socket (``hmi.pub``).  They tolerate unknown fields (``extra="ignore"``)
   so a newer orchestrator does not break an older HMI build.
2. **REST / WebSocket API models** served to the React frontend.

FSM state values mirror ``control_pipeline.schemas.fsm.SystemState`` and link
state values mirror ``control_pipeline.schemas.bridge.LinkState``.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> float:
    return time.time()


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class FsmState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    JAMMED = "jammed"
    SAFETY_FAULT = "safety_fault"
    RECOVERING = "recovering"
    UNKNOWN = "unknown"


class LinkState(StrEnum):
    DOWN = "down"
    SYNC = "sync"
    UP = "up"
    STALE = "stale"
    LOST = "lost"


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class SensorStatus(StrEnum):
    OK = "ok"
    CLEAR = "clear"
    BLOCKED = "blocked"
    ESTIMATED = "estimated"
    WARN = "warn"
    FAULT = "fault"
    OFFLINE = "offline"


class InspectionVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    FAIL_REVIEW = "FAIL_REVIEW"
    BOARD_NOT_ALIGNED = "BOARD_NOT_ALIGNED"


class SlotStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    UNCERTAIN = "uncertain"


class ContainerStatus(StrEnum):
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Inbound wire contracts (orchestrator -> HMI, topic prefix "hmi.")
# ---------------------------------------------------------------------------


_KNOWN_SENSOR_LABELS: dict[str, str] = {
    "motor_driver": "Motor Driver",
    "rotary_encoder": "Rotary Encoder",
    "photoelectric": "Photoelectric Sensor",
    "current_sensor": "Current Sensor",
    "temperature_sensor": "Temperature Sensor",
}


def _coerce_sensor_status(raw: Any) -> SensorStatus:
    text = str(raw or "offline").strip().lower()
    return SensorStatus(text) if text in SensorStatus._value2member_map_ else SensorStatus.OFFLINE


class SensorItem(_Wire):
    """Single interchangeable sensor pill shown in the HMI sub-header strip."""

    id: str
    label: str
    status: SensorStatus = SensorStatus.OFFLINE


class TelemetrySnapshot(_Wire):
    """Topic ``hmi.telemetry`` — conveyor telemetry (target <= 100 ms period).

    ``sensors`` accepts either:
      * a list of ``{"id", "label", "status"}`` objects (arbitrary sensor set), or
      * a ``{sensor_id: status}`` dict (labels inferred from known IDs or title-cased).
    """

    ts: float = Field(default_factory=_now)
    belt_speed_mms: float | None = None
    speed_setpoint_mms: float | None = None
    speed_estimated: bool = False
    motor_current_a: float | None = None
    motor_current_limit_a: float | None = None
    motor_load_pct: float | None = None
    motor_temp_c: float | None = None
    motor_temp_limit_c: float | None = None
    ambient_temp_c: float | None = None
    pwm_duty_pct: float | None = None
    estop_closed: bool | None = None
    pe_clear: bool | None = None
    fault_latched: bool | None = None
    motor_enabled: bool | None = None
    led_on: bool | None = None
    sensors: list[SensorItem] = Field(default_factory=list)

    @field_validator("sensors", mode="before")
    @classmethod
    def _normalise_sensors(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, dict):
            return [
                {
                    "id": str(k),
                    "label": _KNOWN_SENSOR_LABELS.get(str(k), str(k).replace("_", " ").title()),
                    "status": _coerce_sensor_status(v),
                }
                for k, v in value.items()
            ]
        if isinstance(value, list):
            out: list[Any] = []
            for item in value:
                if isinstance(item, dict):
                    sid = str(item.get("id") or item.get("label") or "sensor").lower().replace(" ", "_")
                    label = str(item.get("label") or _KNOWN_SENSOR_LABELS.get(sid, sid.replace("_", " ").title()))
                    out.append({**item, "id": sid, "label": label, "status": _coerce_sensor_status(item.get("status"))})
                else:
                    out.append(item)
            return out
        return []


class StateChange(_Wire):
    """Topic ``hmi.state`` — FSM transition (mirrors ``FsmStateChanged``)."""

    ts: float = Field(default_factory=_now)
    previous: FsmState | None = None
    current: FsmState
    trigger: str | None = None
    origin: str | None = None
    reason: str | None = None


class HmiEvent(_Wire):
    """Topic ``hmi.event`` — operator-facing audit / explainability event."""

    event_id: str
    ts: float = Field(default_factory=_now)
    severity: Severity = Severity.INFO
    source: str = "system"
    description: str
    station_id: str | None = None
    pcb_id: str | None = None
    reason_code: str | None = None
    telemetry: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    snapshot: bool = False


class SlotResult(_Wire):
    slot_id: str
    label: str | None = None
    status: SlotStatus
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="Normalised [x, y, w, h] in 0..1 frame coordinates"
    )


class InspectionResult(_Wire):
    """Topic ``hmi.inspection`` — per-board result from container-cv."""

    ts: float = Field(default_factory=_now)
    pcb_id: str | None = None
    verdict: InspectionVerdict
    slots: list[SlotResult] = Field(default_factory=list)
    latency_ms: float | None = None
    model: str | None = None
    event_id: str | None = None


class VoiceEntry(_Wire):
    """Topic ``hmi.voice`` — dialogue line (operator utterance or assistant reply)."""

    ts: float = Field(default_factory=_now)
    speaker: Literal["operator", "assistant", "system"]
    text: str
    intent: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class VoiceStatus(_Wire):
    """Topic ``hmi.voice_status`` — microphone / listening state."""

    ts: float = Field(default_factory=_now)
    mic_active: bool = False
    listening: bool = False
    noise_floor_dba: float | None = None


class ContainerHealth(_Wire):
    name: str
    status: ContainerStatus = ContainerStatus.RUNNING
    cpu_pct: float | None = None
    ram_mb: float | None = None
    ram_limit_mb: float | None = None
    uptime_s: float | None = None


class HostDiagnostics(_Wire):
    model: str = "Qualcomm QRB2210"
    cpu_pct: float | None = None
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    storage_used_gb: float | None = None
    storage_total_gb: float | None = None
    temp_c: float | None = None
    containers: list[ContainerHealth] = Field(default_factory=list)


class LatencyBin(_Wire):
    label: str
    count: int = 0


class BridgeDiagnostics(_Wire):
    transport: str = "Arduino Bridge RPC"
    link_state: LinkState = LinkState.DOWN
    baud: int | None = None
    heartbeat_period_ms: float | None = None
    heartbeat_age_ms: float | None = None
    rtt_ms: float | None = None
    rtt_mean_ms: float | None = None
    rtt_std_ms: float | None = None
    command_ttl_ms: float | None = None
    link_loss_timeout_ms: float | None = None
    histogram: list[LatencyBin] = Field(default_factory=list)
    frames_total: int | None = None
    crc_pass: int | None = None
    dropped: int | None = None
    tx_buffer_used: int | None = None
    tx_buffer_size: int | None = None
    rx_buffer_used: int | None = None
    rx_buffer_size: int | None = None


class McuIo(_Wire):
    port: str
    function: str
    value: str | None = None
    state: str = "unknown"


class PowerRail(_Wire):
    name: str
    nominal_v: float
    measured_v: float | None = None


class McuDiagnostics(_Wire):
    model: str = "STM32U585"
    io: list[McuIo] = Field(default_factory=list)
    power: list[PowerRail] = Field(default_factory=list)


class DiagnosticsSnapshot(_Wire):
    """Topic ``hmi.diagnostics`` — host/bridge/MCU health (target ~1 s period)."""

    ts: float = Field(default_factory=_now)
    host: HostDiagnostics = Field(default_factory=HostDiagnostics)
    bridge: BridgeDiagnostics = Field(default_factory=BridgeDiagnostics)
    mcu: McuDiagnostics = Field(default_factory=McuDiagnostics)


class FrameMeta(_Wire):
    """Topic ``hmi.frame`` (part 2 of 3) — metadata for the JPEG in part 3."""

    ts: float = Field(default_factory=_now)
    camera_id: str = "cam0"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    latency_ms: float | None = None
    led_on: bool | None = None


class SnapshotMeta(_Wire):
    """Topic ``hmi.snapshot`` (part 2 of 3) — evidence JPEG bound to an event."""

    event_id: str
    ts: float = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# REST / WebSocket API models
# ---------------------------------------------------------------------------


class StationInfo(BaseModel):
    id: str
    name: str
    host: str


class CameraStatus(BaseModel):
    online: bool = False
    camera_id: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    latency_ms: float | None = None
    led_on: bool | None = None
    last_frame_at: float | None = None


class ConnectionStatus(BaseModel):
    orchestrator_connected: bool = False
    last_message_at: float | None = None
    command_channel_ready: bool = False
    database_available: bool = False


class TelemetryPoint(BaseModel):
    ts: float
    speed: float | None = None
    current: float | None = None
    temp: float | None = None


class HmiSettings(BaseModel):
    voice_sensitivity_pct: int = Field(default=80, ge=50, le=95)
    defect_threshold_pct: int = Field(default=80, ge=50, le=95)
    tts_enabled: bool = True
    updated_at: float | None = None


class SettingsUpdate(BaseModel):
    voice_sensitivity_pct: int | None = Field(default=None, ge=50, le=95)
    defect_threshold_pct: int | None = Field(default=None, ge=50, le=95)
    tts_enabled: bool | None = None


class SpeedLimits(BaseModel):
    min_mms: int
    max_mms: int
    step_mms: int


class TelemetryThresholds(BaseModel):
    motor_current_limit_a: float = 2.5
    motor_temp_limit_c: float = 80.0
    default_setpoint_mms: int = 120


class CalibrationBounds(BaseModel):
    voice_min_pct: int = 50
    voice_max_pct: int = 95
    defect_min_pct: int = 50
    defect_max_pct: int = 95


class DiagnosticsDefaults(BaseModel):
    host_model: str = "Qualcomm QRB2210"
    ram_total_mb: int = 4096
    storage_total_gb: float = 32.0
    containers: list[str] = Field(default_factory=list)
    bridge_transport: str = "Arduino Bridge RPC"
    heartbeat_base_label: str = "200 Hz Base"
    rtt_limit_ms: float = 15.0
    command_ttl_ms: int = 200
    link_loss_timeout_ms: int = 300
    buffer_size_bytes: int = 4096
    histogram_window_label: str = "last 10k frames"
    histogram_bins: list[str] = Field(default_factory=list)
    mcu_model: str = "STM32U585"
    mcu_io: list[McuIo] = Field(default_factory=list)
    power_rails: list[PowerRail] = Field(default_factory=list)


class CloudStatus(BaseModel):
    enabled: bool = False
    status: Literal["deferred", "disconnected", "connected"] = "disconnected"
    queue_depth: int | None = None
    buffer_used_mb: float | None = None
    buffer_limit_gb: float | None = None
    sync_label: str = "Offline"
    last_sync_at: float | None = None
    note: str = "Cloud telemetry egress (Project B / CH-41)."


class StagedUpdate(BaseModel):
    version: str
    status: str = "READY TO INSTALL"
    changelog: str = ""
    release_notes: str = ""


class UpdateStatus(BaseModel):
    enabled: bool = False
    current_version: str | None = None
    build: str | None = None
    available: StagedUpdate | None = None
    rollback_versions: list[str] = Field(default_factory=list)
    note: str = "No Update Available"


class SystemInfo(BaseModel):
    station: StationInfo
    stations: list[StationInfo]
    sensors: list[SensorItem] = Field(default_factory=list)
    operator: str
    speed_limits: SpeedLimits
    thresholds: TelemetryThresholds = Field(default_factory=TelemetryThresholds)
    calibration_bounds: CalibrationBounds = Field(default_factory=CalibrationBounds)
    diagnostics_defaults: DiagnosticsDefaults = Field(default_factory=DiagnosticsDefaults)
    cloud: CloudStatus
    updates: UpdateStatus
    server_time: float = Field(default_factory=_now)


class StateSnapshot(BaseModel):
    """Full state sent on WebSocket connect and served by ``GET /api/state``."""

    station: StationInfo
    fsm: StateChange | None = None
    telemetry: TelemetrySnapshot | None = None
    history: list[TelemetryPoint] = Field(default_factory=list)
    events: list[HmiEvent] = Field(default_factory=list)
    inspection: InspectionResult | None = None
    voice_status: VoiceStatus | None = None
    voice: list[VoiceEntry] = Field(default_factory=list)
    diagnostics: DiagnosticsSnapshot | None = None
    camera: CameraStatus = Field(default_factory=CameraStatus)
    connection: ConnectionStatus = Field(default_factory=ConnectionStatus)
    settings: HmiSettings = Field(default_factory=HmiSettings)


CommandAction = Literal[
    "start",
    "stop",
    "pause",
    "resume",
    "set_speed",
    "verify_clearance",
    "fault_cleared",
]


class CommandRequest(BaseModel):
    action: CommandAction
    setpoint_mms: int | None = Field(default=None, ge=0)
    confirmed: bool = Field(
        default=False,
        description="Operator two-step confirmation; required for start/resume (FR-06).",
    )


class CommandResponse(BaseModel):
    accepted: bool
    command_id: str
    action: CommandAction
    detail: str
    sent_at: float = Field(default_factory=_now)


class LogEntry(BaseModel):
    id: str
    ts: float
    severity: Severity
    source: str
    description: str
    event_type: str | None = None
    station_id: str | None = None
    pcb_id: str | None = None
    telemetry: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    has_snapshot: bool = False


class LogPage(BaseModel):
    items: list[LogEntry]
    total: int
    page: int
    page_size: int
    backend: Literal["sqlite", "memory"]


class WsMessage(BaseModel):
    type: str
    data: Any = None
    ts: float = Field(default_factory=_now)
