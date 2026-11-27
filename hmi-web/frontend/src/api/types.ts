// Mirrors hmi-web/backend/app/schemas.py (REST + WebSocket contracts).

export type FsmState =
  | "idle"
  | "running"
  | "paused"
  | "jammed"
  | "safety_fault"
  | "recovering"
  | "unknown";

export type LinkState = "down" | "sync" | "up" | "stale" | "lost";
export type Severity = "INFO" | "WARN" | "CRITICAL";
export type SensorStatus = "ok" | "clear" | "blocked" | "estimated" | "warn" | "fault" | "offline";
export type InspectionVerdict = "PASS" | "FAIL" | "FAIL_REVIEW" | "BOARD_NOT_ALIGNED";
export type SlotStatus = "present" | "missing" | "uncertain";
export type ContainerStatus = "running" | "degraded" | "stopped" | "disabled";

export interface StationInfo {
  id: string;
  name: string;
  host: string;
}

export interface SensorItem {
  id: string;
  label: string;
  status: SensorStatus;
}

export interface TelemetrySnapshot {
  ts: number;
  belt_speed_mms: number | null;
  speed_setpoint_mms: number | null;
  speed_estimated: boolean;
  motor_current_a: number | null;
  motor_current_limit_a: number | null;
  motor_load_pct: number | null;
  motor_temp_c: number | null;
  motor_temp_limit_c: number | null;
  ambient_temp_c: number | null;
  pwm_duty_pct: number | null;
  estop_closed: boolean | null;
  pe_clear: boolean | null;
  fault_latched: boolean | null;
  motor_enabled: boolean | null;
  led_on: boolean | null;
  sensors: SensorItem[];
}

export interface TelemetryPoint {
  ts: number;
  speed: number | null;
  current: number | null;
  temp: number | null;
}

export interface StateChange {
  ts: number;
  previous: FsmState | null;
  current: FsmState;
  trigger: string | null;
  origin: string | null;
  reason: string | null;
}

export interface HmiEvent {
  event_id: string;
  ts: number;
  severity: Severity;
  source: string;
  description: string;
  station_id: string | null;
  pcb_id: string | null;
  reason_code: string | null;
  telemetry: Record<string, unknown> | null;
  payload: Record<string, unknown> | null;
  snapshot: boolean;
}

export interface SlotResult {
  slot_id: string;
  label: string | null;
  status: SlotStatus;
  confidence: number | null;
  bbox: [number, number, number, number] | null;
}

export interface InspectionResult {
  ts: number;
  pcb_id: string | null;
  verdict: InspectionVerdict;
  slots: SlotResult[];
  latency_ms: number | null;
  model: string | null;
  event_id: string | null;
}

export interface VoiceEntry {
  ts: number;
  speaker: "operator" | "assistant" | "system";
  text: string;
  intent: string | null;
  confidence: number | null;
}

export interface VoiceStatus {
  ts: number;
  mic_active: boolean;
  listening: boolean;
  noise_floor_dba: number | null;
}

export interface ContainerHealth {
  name: string;
  status: ContainerStatus;
  cpu_pct: number | null;
  ram_mb: number | null;
  ram_limit_mb: number | null;
  uptime_s: number | null;
}

export interface HostDiagnostics {
  model: string;
  cpu_pct: number | null;
  ram_used_mb: number | null;
  ram_total_mb: number | null;
  storage_used_gb: number | null;
  storage_total_gb: number | null;
  temp_c: number | null;
  containers: ContainerHealth[];
}

export interface LatencyBin {
  label: string;
  count: number;
}

export interface BridgeDiagnostics {
  transport: string;
  link_state: LinkState;
  baud: number | null;
  heartbeat_period_ms: number | null;
  heartbeat_age_ms: number | null;
  rtt_ms: number | null;
  rtt_mean_ms: number | null;
  rtt_std_ms: number | null;
  command_ttl_ms: number | null;
  link_loss_timeout_ms: number | null;
  histogram: LatencyBin[];
  frames_total: number | null;
  crc_pass: number | null;
  dropped: number | null;
  tx_buffer_used: number | null;
  tx_buffer_size: number | null;
  rx_buffer_used: number | null;
  rx_buffer_size: number | null;
}

export interface McuIo {
  port: string;
  function: string;
  value: string | null;
  state: string;
}

export interface PowerRail {
  name: string;
  nominal_v: number;
  measured_v: number | null;
}

export interface McuDiagnostics {
  model: string;
  io: McuIo[];
  power: PowerRail[];
}

export interface DiagnosticsSnapshot {
  ts: number;
  host: HostDiagnostics;
  bridge: BridgeDiagnostics;
  mcu: McuDiagnostics;
}

export interface CameraStatus {
  online: boolean;
  camera_id: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  latency_ms: number | null;
  led_on: boolean | null;
  last_frame_at: number | null;
}

export interface ConnectionStatus {
  orchestrator_connected: boolean;
  last_message_at: number | null;
  command_channel_ready: boolean;
  database_available: boolean;
}

export interface HmiSettings {
  voice_sensitivity_pct: number;
  defect_threshold_pct: number;
  tts_enabled: boolean;
  updated_at: number | null;
}

export interface SpeedLimits {
  min_mms: number;
  max_mms: number;
  step_mms: number;
}

export interface TelemetryThresholds {
  motor_current_limit_a: number;
  motor_temp_limit_c: number;
  default_setpoint_mms: number;
}

export interface CalibrationBounds {
  voice_min_pct: number;
  voice_max_pct: number;
  defect_min_pct: number;
  defect_max_pct: number;
}

export interface DiagnosticsDefaults {
  host_model: string;
  ram_total_mb: number;
  storage_total_gb: number;
  containers: string[];
  bridge_transport: string;
  heartbeat_base_label: string;
  rtt_limit_ms: number;
  command_ttl_ms: number;
  link_loss_timeout_ms: number;
  buffer_size_bytes: number;
  histogram_window_label: string;
  histogram_bins: string[];
  mcu_model: string;
  mcu_io: McuIo[];
  power_rails: PowerRail[];
}

export interface CloudStatus {
  enabled: boolean;
  status: "deferred" | "disconnected" | "connected";
  queue_depth: number | null;
  buffer_used_mb: number | null;
  buffer_limit_gb: number | null;
  sync_label: string;
  last_sync_at: number | null;
  note: string;
}

export interface StagedUpdate {
  version: string;
  status: string;
  changelog: string;
  release_notes: string;
}

export interface UpdateStatus {
  enabled: boolean;
  current_version: string | null;
  build: string | null;
  available: StagedUpdate | null;
  rollback_versions: string[];
  note: string;
}

export interface SystemInfo {
  station: StationInfo;
  stations: StationInfo[];
  sensors: SensorItem[];
  operator: string;
  speed_limits: SpeedLimits;
  thresholds: TelemetryThresholds;
  calibration_bounds: CalibrationBounds;
  diagnostics_defaults: DiagnosticsDefaults;
  cloud: CloudStatus;
  updates: UpdateStatus;
  server_time: number;
}

export interface StateSnapshot {
  station: StationInfo;
  fsm: StateChange | null;
  telemetry: TelemetrySnapshot | null;
  history: TelemetryPoint[];
  events: HmiEvent[];
  inspection: InspectionResult | null;
  voice_status: VoiceStatus | null;
  voice: VoiceEntry[];
  diagnostics: DiagnosticsSnapshot | null;
  camera: CameraStatus;
  connection: ConnectionStatus;
  settings: HmiSettings;
}

export type CommandAction =
  | "start"
  | "stop"
  | "pause"
  | "resume"
  | "set_speed"
  | "verify_clearance"
  | "fault_cleared";

export interface CommandRequest {
  action: CommandAction;
  setpoint_mms?: number;
  confirmed?: boolean;
}

export interface CommandResponse {
  accepted: boolean;
  command_id: string;
  action: CommandAction;
  detail: string;
  sent_at: number;
}

export interface LogEntry {
  id: string;
  ts: number;
  severity: Severity;
  source: string;
  description: string;
  event_type: string | null;
  station_id: string | null;
  pcb_id: string | null;
  telemetry: Record<string, unknown> | null;
  payload: Record<string, unknown> | null;
  has_snapshot: boolean;
}

export interface LogPage {
  items: LogEntry[];
  total: number;
  page: number;
  page_size: number;
  backend: "sqlite" | "memory";
}

export interface LogFilters {
  page: number;
  pageSize: number;
  severity: Severity[];
  source: string;
  search: string;
}

export type WsMessage =
  | { type: "snapshot"; data: StateSnapshot; ts: number }
  | { type: "telemetry"; data: TelemetrySnapshot; ts: number }
  | { type: "state"; data: StateChange; ts: number }
  | { type: "event"; data: HmiEvent; ts: number }
  | { type: "inspection"; data: InspectionResult; ts: number }
  | { type: "voice"; data: VoiceEntry; ts: number }
  | { type: "voice_status"; data: VoiceStatus; ts: number }
  | { type: "diagnostics"; data: DiagnosticsSnapshot; ts: number }
  | { type: "camera"; data: CameraStatus; ts: number }
  | { type: "connection"; data: ConnectionStatus; ts: number }
  | { type: "settings"; data: HmiSettings; ts: number }
  | { type: "snapshot_ready"; data: { event_id: string }; ts: number }
  | { type: "pong"; data: null; ts?: number };
