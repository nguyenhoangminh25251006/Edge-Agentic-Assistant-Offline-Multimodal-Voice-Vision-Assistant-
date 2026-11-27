import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  AudioLines,
  Bot,
  Check,
  Filter,
  Mic,
  Minus,
  Pause,
  Play,
  Plus,
  ShieldCheck,
  Square,
  Sun,
  User,
} from "lucide-react";
import { clsx } from "clsx";
import { ApiError, api } from "../api/client";
import type { CommandAction, SensorItem, Severity } from "../api/types";
import { ConfirmModal } from "../components/ConfirmModal";
import { Sparkline } from "../components/Sparkline";
import { StationBar } from "../components/StationBar";
import { SENSOR_LABEL, SENSOR_TONE, fmtClockUtc, fmtNumber, type Tone } from "../lib/format";
import { useHmi } from "../store/HmiContext";

const PILL_BORDER: Record<Tone, { box: string; dot: string; val: string }> = {
  ok: { box: "border-[#22c55e] bg-white", dot: "bg-[#16a34a]", val: "text-[#15803d]" },
  warn: { box: "border-[#f59e0b] bg-white", dot: "bg-[#d97706]", val: "text-[#b45309]" },
  crit: { box: "border-[#ef4444] bg-white", dot: "bg-[#dc2626]", val: "text-[#b91c1c]" },
  info: { box: "border-[#3b82f6] bg-white", dot: "bg-[#2563eb]", val: "text-[#1d4ed8]" },
  muted: { box: "border-[#cbd5e1] bg-white", dot: "bg-slate-400", val: "text-slate-500" },
};

const LEVEL_BADGE: Record<Severity, string> = {
  INFO: "border-[#22c55e] bg-white text-[#15803d]",
  WARN: "border-[#f59e0b] bg-[#fffbeb] text-[#b45309]",
  CRITICAL: "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
};

const SOURCE_BADGE: Record<Severity, string> = {
  INFO: "border-[#cbd5e1] bg-[#f8fafc] text-[#334155]",
  WARN: "border-[#f59e0b] bg-[#fffbeb] text-[#b45309]",
  CRITICAL: "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
};

export function LiveOverviewPage() {
  const navigate = useNavigate();
  const { station, fsm, telemetry, history, events, inspection, voiceStatus, voice, camera, system } =
    useHmi();

  const limits = system?.speed_limits;
  const thresholds = system?.thresholds;
  const [setpoint, setSetpoint] = useState<number>(thresholds?.default_setpoint_mms ?? 120);
  const [setpointTouched, setSetpointTouched] = useState(false);
  const [severityFilter, setSeverityFilter] = useState<Severity | "ALL">("ALL");
  const [filterOpen, setFilterOpen] = useState(false);
  const [trailPaused, setTrailPaused] = useState(false);
  const [frozenEvents, setFrozenEvents] = useState(events);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState<{
    action: CommandAction;
    title: string;
    description: string;
    confirmLabel: string;
  } | null>(null);

  const voiceEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (telemetry?.speed_setpoint_mms) {
      setSetpoint(Math.round(telemetry.speed_setpoint_mms));
    } else if (!setpointTouched && system?.thresholds.default_setpoint_mms) {
      setSetpoint(system.thresholds.default_setpoint_mms);
    }
  }, [telemetry?.speed_setpoint_mms, system?.thresholds.default_setpoint_mms, setpointTouched]);

  useEffect(() => {
    if (!trailPaused) setFrozenEvents(events);
  }, [events, trailPaused]);

  useEffect(() => {
    voiceEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [voice.length]);

  if (!limits || !thresholds) return null;

  const fsmState = fsm?.current ?? "unknown";
  const ledOn = telemetry?.led_on ?? camera.led_on;

  const sensorPills: SensorItem[] = useMemo(() => {
    const base =
      telemetry && telemetry.sensors.length > 0 ? telemetry.sensors : (system?.sensors ?? []);
    if (!telemetry?.speed_estimated) return base;
    return base.map((item) =>
      item.id.includes("encoder") && item.status !== "fault"
        ? { ...item, status: "estimated" }
        : item,
    );
  }, [telemetry, system?.sensors]);

  const sourceList = trailPaused ? frozenEvents : events;
  const visibleEvents = useMemo(() => {
    const filtered =
      severityFilter === "ALL" ? sourceList : sourceList.filter((e) => e.severity === severityFilter);
    return filtered.slice(-7).reverse();
  }, [sourceList, severityFilter]);

  const speedDeltaPct = useMemo(() => {
    if (
      telemetry?.belt_speed_mms === null ||
      telemetry?.belt_speed_mms === undefined ||
      !telemetry.speed_setpoint_mms
    ) {
      return null;
    }
    return ((telemetry.belt_speed_mms - telemetry.speed_setpoint_mms) / telemetry.speed_setpoint_mms) * 100;
  }, [telemetry]);

  const currentLimit = telemetry?.motor_current_limit_a ?? thresholds.motor_current_limit_a;
  const tempLimit = telemetry?.motor_temp_limit_c ?? thresholds.motor_temp_limit_c;
  const loadPct =
    telemetry?.motor_load_pct ??
    (telemetry?.motor_current_a !== null && telemetry?.motor_current_a !== undefined
      ? Math.min(100, (telemetry.motor_current_a / currentLimit) * 100)
      : null);

  async function dispatchCommand(action: CommandAction, confirmed = false, customSetpoint?: number) {
    setBusy(true);
    setFeedback(null);
    try {
      const res = await api.sendCommand({
        action,
        setpoint_mms: customSetpoint,
        confirmed,
      });
      setFeedback({ ok: true, text: res.detail });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to reach HMI gateway";
      setFeedback({ ok: false, text: msg });
    } finally {
      setBusy(false);
    }
  }

  function handlePrimaryAction() {
    if (fsmState === "running") {
      void dispatchCommand("pause");
      return;
    }
    if (fsmState === "paused") {
      setPendingConfirm({
        action: "resume",
        title: "Confirm Conveyor Resume",
        description: `Resume conveyor belt motion at ${setpoint} mm/s?`,
        confirmLabel: "Confirm Resume",
      });
      return;
    }
    if (fsmState === "jammed") {
      setPendingConfirm({
        action: "verify_clearance",
        title: "Verify Jam Clearance",
        description: "Confirm obstruction is physically cleared before transitioning to RECOVERING.",
        confirmLabel: "Confirm Clear",
      });
      return;
    }
    if (fsmState === "safety_fault" || fsmState === "recovering") {
      setPendingConfirm({
        action: "fault_cleared",
        title: "Clear Safety Fault",
        description: "Confirm hardware E-stop is closed and interlocks are clear before returning to IDLE.",
        confirmLabel: "Clear Fault",
      });
      return;
    }
    setPendingConfirm({
      action: "start",
      title: "Confirm Conveyor Start",
      description: `Start conveyor belt on ${station?.id ?? "station"} at ${setpoint} mm/s?`,
      confirmLabel: "Confirm Start",
    });
  }

  const primaryLabel =
    fsmState === "running"
      ? "Pause Belt"
      : fsmState === "paused"
        ? "Resume Belt"
        : fsmState === "jammed"
          ? "Verify Clearance"
          : fsmState === "safety_fault" || fsmState === "recovering"
            ? "Clear Fault"
            : "Start Belt";

  const PrimaryIcon =
    fsmState === "running"
      ? Pause
      : fsmState === "jammed" || fsmState === "safety_fault" || fsmState === "recovering"
        ? ShieldCheck
        : Play;

  return (
    <div className="min-h-[calc(100vh-56px)] flex flex-col">
      <StationBar
        left={sensorPills.map((pill) => {
          const style = PILL_BORDER[SENSOR_TONE[pill.status]];
          return (
            <div
              key={pill.id}
              className={clsx(
                "inline-flex items-center gap-1.5 rounded-[2px] border px-2 py-1 font-mono text-[11px] whitespace-nowrap",
                style.box,
              )}
            >
              <span className="text-slate-700">{pill.label}:</span>
              <span className={clsx("h-2 w-2 rounded-full", style.dot)} />
              <span className={clsx("font-bold", style.val)}>{SENSOR_LABEL[pill.status]}</span>
            </div>
          );
        })}
      />

      <main className="mx-auto w-full max-w-[1600px] flex-1 p-4 grid grid-cols-1 lg:grid-cols-[1.12fr_1fr] gap-4 items-start">
        {/* Left Column: Unified Card (Camera Feed + Voice Agent Dialogue) */}
        <section className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card divide-y divide-[#cbd5e1]">
          {/* Camera Feed Section */}
          <div className="p-3.5 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className={clsx(
                    "h-2.5 w-2.5 rounded-full shrink-0",
                    camera.online ? "bg-[#16a34a]" : "bg-slate-400",
                  )}
                />
                <h2 className="text-[15px] font-bold text-[#0f172a] truncate">
                  Station {station?.id ?? "--"} Live Inspection Camera Feed
                </h2>
              </div>
              <div
                className={clsx(
                  "inline-flex items-center gap-1.5 rounded-[2px] border px-2.5 py-1 font-mono text-[11px] shrink-0",
                  ledOn ? "border-[#22c55e] bg-white" : "border-[#cbd5e1] bg-white",
                )}
              >
                <Sun className={clsx("h-3.5 w-3.5", ledOn ? "text-amber-500" : "text-slate-400")} />
                <span className="text-slate-700">LED Light:</span>
                <span className={clsx("h-1.5 w-1.5 rounded-full", ledOn ? "bg-[#16a34a]" : "bg-slate-400")} />
                <span className={clsx("font-bold", ledOn ? "text-[#15803d]" : "text-slate-500")}>
                  {ledOn === null || ledOn === undefined ? "--" : ledOn ? "ON" : "OFF"}
                </span>
              </div>
            </div>

            <div className="relative aspect-[16/10] w-full overflow-hidden border border-[#94a3b8] bg-[#0b1320]">
              {camera.online ? (
                <img
                  src={api.cameraStreamUrl}
                  alt="Live inspection camera feed"
                  className="h-full w-full object-contain"
                />
              ) : (
                <div className="flex h-full w-full items-center justify-center font-mono text-xs text-slate-400">
                  No camera signal
                </div>
              )}

              {/* Top-right FPS | latency pill */}
              <div className="absolute top-3 right-3 rounded-[2px] border border-[#cbd5e1] bg-white/95 px-2.5 py-1 font-mono text-[11px] shadow-sm tabular">
                <span className={clsx("font-bold", camera.online ? "text-[#15803d]" : "text-slate-500")}>
                  {camera.fps !== null ? `${fmtNumber(camera.fps, 1)} FPS` : "-- FPS"}
                </span>
                <span className="mx-1.5 text-slate-300">|</span>
                <span className="text-slate-600">
                  {inspection?.latency_ms !== null && inspection?.latency_ms !== undefined
                    ? `${fmtNumber(inspection.latency_ms, 0)}ms latency`
                    : camera.latency_ms !== null
                      ? `${fmtNumber(camera.latency_ms, 0)}ms latency`
                      : "--ms latency"}
                </span>
              </div>

              {/* Per-slot defect overlay */}
              {inspection?.slots.map((slot) => {
                if (!slot.bbox) return null;
                const [x, y, w, h] = slot.bbox;
                if (slot.status === "present") return null;
                return (
                  <div
                    key={slot.slot_id}
                    style={{
                      left: `${x * 100}%`,
                      top: `${y * 100}%`,
                      width: `${w * 100}%`,
                      height: `${h * 100}%`,
                    }}
                    className={clsx(
                      "absolute border-2 pointer-events-none",
                      slot.status === "missing"
                        ? "border-red-500 bg-red-500/20"
                        : "border-amber-400 bg-amber-400/20",
                    )}
                  >
                    <div className="absolute -top-7 left-0 whitespace-nowrap border border-[#dc2626] bg-white px-2 py-0.5 font-mono text-[11px] font-bold text-[#b91c1c] shadow">
                      <AlertTriangle className="mr-1 inline h-3.5 w-3.5" />
                      DEFECT: [{inspection.pcb_id ?? "PCB"}] {slot.label ?? slot.slot_id}
                      {slot.confidence !== null ? ` [${fmtNumber(slot.confidence * 100, 1)}%]` : ""}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Voice Agent Dialogue Section */}
          <div className="p-3.5 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span
                  className={clsx(
                    "h-2.5 w-2.5 rounded-full",
                    voiceStatus?.mic_active ? "bg-[#16a34a]" : "bg-slate-400",
                  )}
                />
                <h2 className="text-[15px] font-bold text-[#0f172a]">Voice Agent Dialogue</h2>
              </div>
              <div className="flex items-center gap-3 font-mono text-[11px]">
                <span
                  className={clsx(
                    "inline-flex items-center gap-1.5 rounded-[2px] border px-2 py-0.5 font-semibold",
                    voiceStatus?.mic_active
                      ? "border-[#22c55e] bg-[#ecfdf3] text-[#15803d]"
                      : "border-[#cbd5e1] bg-white text-slate-500",
                  )}
                >
                  <Mic className="h-3 w-3" />
                  Microphone: {voiceStatus?.mic_active ? "ACTIVE" : "IDLE"}
                </span>
                <span className="text-slate-600 tabular">
                  Noise Floor:{" "}
                  {voiceStatus?.noise_floor_dba !== null && voiceStatus?.noise_floor_dba !== undefined
                    ? `${fmtNumber(voiceStatus.noise_floor_dba, 0)} dBA`
                    : "-- dBA"}
                </span>
              </div>
            </div>

            <div className="h-44 overflow-y-auto space-y-2.5 pr-1">
              {voice.map((entry, idx) => {
                const isOp = entry.speaker === "operator";
                return (
                  <div key={`${entry.ts}-${idx}`} className={clsx("flex", isOp ? "justify-end" : "justify-start")}>
                    <div
                      className={clsx(
                        "max-w-[85%] rounded-[2px] border px-3.5 py-2.5 font-mono text-xs",
                        isOp
                          ? "border-[#cbd5e1] bg-[#f4f8fd] text-[#1e3a8a]"
                          : "border-[#cbd5e1] bg-white text-[#0f172a]",
                      )}
                    >
                      <div className="flex items-start gap-2 font-semibold leading-snug">
                        {isOp ? (
                          <User className="h-3.5 w-3.5 mt-0.5 shrink-0 text-[#1e3a8a]" />
                        ) : (
                          <Bot className="h-3.5 w-3.5 mt-0.5 shrink-0 text-[#15803d]" />
                        )}
                        <span>
                          {isOp ? "Operator: " : entry.speaker === "assistant" ? "EAA Assistant: " : ""}
                          {entry.text}
                        </span>
                      </div>
                      <div
                        className={clsx(
                          "mt-1.5 text-[10px] text-slate-500 tabular",
                          isOp ? "text-right" : "text-left",
                        )}
                      >
                        {fmtClockUtc(entry.ts)}
                      </div>
                    </div>
                  </div>
                );
              })}
              <div ref={voiceEndRef} />
            </div>

            <div className="flex items-center gap-2 rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] px-3 py-1.5 font-mono text-xs">
              <AudioLines className={clsx("h-3.5 w-3.5", voiceStatus?.listening ? "text-[#15803d]" : "text-slate-400")} />
              <span className={voiceStatus?.listening ? "text-[#15803d] font-medium" : "text-slate-500"}>
                {voiceStatus?.listening ? "Listening..." : "Standby..."}
              </span>
            </div>
          </div>
        </section>

        {/* Right Column: 3 Hero Metrics + Live Audit Trail + Conveyor Control Panel */}
        <div className="flex flex-col gap-4">
          {/* 3 Metric Cards */}
          <div className="grid grid-cols-3 gap-3.5">
            <div className="rounded-[3px] border border-[#cbd5e1] bg-white p-3.5 shadow-card flex flex-col justify-between gap-2.5">
              <div className="flex items-center justify-between gap-1">
                <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  BELT SPEED
                </span>
                {telemetry?.speed_estimated && (
                  <span className="rounded-[2px] border border-[#f59e0b] bg-[#fffbeb] px-1 py-0.5 font-mono text-[9px] font-bold text-[#b45309]">
                    EST
                  </span>
                )}
              </div>
              <div className="flex items-baseline gap-1 font-mono tabular">
                <span className="text-[28px] font-bold leading-none text-[#0f172a]">
                  {fmtNumber(telemetry?.belt_speed_mms, 0)}
                </span>
                <span className="text-xs text-slate-500">mm/s</span>
              </div>
              <div className="flex items-center justify-between gap-2 pt-1">
                <Sparkline values={history.map((p) => p.speed)} width={76} height={18} stroke="#16a34a" />
                <span className="font-mono text-[11px] text-slate-500 tabular">
                  {speedDeltaPct !== null ? `Δ ${speedDeltaPct >= 0 ? "+" : ""}${speedDeltaPct.toFixed(1)}%` : "Δ --"}
                </span>
              </div>
            </div>

            <div className="rounded-[3px] border border-[#cbd5e1] bg-white p-3.5 shadow-card flex flex-col justify-between gap-2.5">
              <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                MOTOR LOAD
              </span>
              <div className="flex items-baseline gap-1 font-mono tabular">
                <span className="text-[28px] font-bold leading-none text-[#0f172a]">
                  {fmtNumber(telemetry?.motor_current_a, 2)}
                </span>
                <span className="text-xs text-slate-500">A</span>
              </div>
              <div className="space-y-1.5">
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-[#dce4ee]">
                  <div
                    className="h-full bg-[#1e3a8a] transition-all"
                    style={{ width: `${Math.min(100, loadPct ?? 0)}%` }}
                  />
                </div>
                <div className="flex items-center justify-between font-mono text-[11px] tabular">
                  <span className="font-bold text-[#0f172a]">{loadPct !== null ? `${loadPct.toFixed(1)}%` : "--%"}</span>
                  <span className="text-slate-500">Limit: {fmtNumber(currentLimit, 1)}A</span>
                </div>
              </div>
            </div>

            <div className="rounded-[3px] border border-[#cbd5e1] bg-white p-3.5 shadow-card flex flex-col justify-between gap-2.5">
              <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                MOTOR TEMP
              </span>
              <div className="flex items-baseline gap-1 font-mono tabular">
                <span className="text-[28px] font-bold leading-none text-[#0f172a]">
                  {fmtNumber(telemetry?.motor_temp_c, 1)}
                </span>
                <span className="text-xs text-slate-500">°C</span>
              </div>
              <div className="flex items-center justify-between font-mono text-[11px] text-slate-500 tabular pt-1">
                <span className="inline-flex items-center gap-1.5">
                  <span
                    className={clsx(
                      "h-2 w-2 rounded-full",
                      telemetry?.motor_temp_c !== null &&
                        telemetry?.motor_temp_c !== undefined &&
                        telemetry.motor_temp_c >= tempLimit
                        ? "bg-[#dc2626]"
                        : "bg-[#16a34a]",
                    )}
                  />
                  &lt;{fmtNumber(tempLimit, 0)}°C
                </span>
                <span>Amb: {fmtNumber(telemetry?.ambient_temp_c, 0)}°C</span>
              </div>
            </div>
          </div>

          {/* Live Audit Trail */}
          <section className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card overflow-hidden">
            <header className="flex items-center justify-between gap-2 border-b border-[#cbd5e1] px-4 py-2.5">
              <h2 className="text-[15px] font-bold text-[#0f172a]">Live Audit Trail</h2>
              <div className="relative flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setFilterOpen((o) => !o)}
                  className={clsx(
                    "inline-flex items-center gap-1.5 rounded-[2px] border px-2.5 py-1 font-mono text-[11px] font-medium",
                    severityFilter !== "ALL"
                      ? "border-[#1e3a8a] bg-[#eff6ff] text-[#1e3a8a]"
                      : "border-[#b8c4d4] bg-white text-[#334155] hover:bg-slate-50",
                  )}
                >
                  <Filter className="h-3 w-3" />
                  <span>{severityFilter === "ALL" ? "Filter" : severityFilter}</span>
                </button>
                {filterOpen && (
                  <div className="absolute right-8 top-8 z-20 w-32 rounded-[2px] border border-[#b8c4d4] bg-white py-1 shadow-md font-mono text-xs">
                    {(["ALL", "CRITICAL", "WARN", "INFO"] as const).map((lvl) => (
                      <button
                        key={lvl}
                        type="button"
                        onClick={() => {
                          setSeverityFilter(lvl);
                          setFilterOpen(false);
                        }}
                        className={clsx(
                          "block w-full px-3 py-1.5 text-left hover:bg-slate-100",
                          severityFilter === lvl && "font-bold text-[#1e3a8a] bg-[#eff6ff]",
                        )}
                      >
                        {lvl === "ALL" ? "All Levels" : lvl}
                      </button>
                    ))}
                  </div>
                )}
                <button
                  type="button"
                  title={trailPaused ? "Resume live trail" : "Pause live trail"}
                  aria-label={trailPaused ? "Resume live trail" : "Pause live trail"}
                  onClick={() => setTrailPaused((p) => !p)}
                  className={clsx(
                    "inline-flex h-7 w-7 items-center justify-center rounded-[2px] border",
                    trailPaused
                      ? "border-[#f59e0b] bg-[#fffbeb] text-[#b45309]"
                      : "border-[#b8c4d4] bg-white text-[#334155] hover:bg-slate-50",
                  )}
                >
                  {trailPaused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />}
                </button>
              </div>
            </header>

            <div className="overflow-x-auto">
              <table className="w-full text-left font-mono text-[11px]">
                <thead className="border-b border-[#cbd5e1] bg-[#f4f7fa] text-[10px] font-bold uppercase tracking-wider text-slate-600">
                  <tr>
                    <th className="px-3.5 py-2">TIMESTAMP</th>
                    <th className="px-2 py-2">LEVEL</th>
                    <th className="px-2 py-2">DESCRIPTION</th>
                    <th className="px-3.5 py-2 text-right">SOURCE</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#e2e8f0]">
                  {visibleEvents.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="px-4 py-10 text-center text-slate-400">
                        No events logged yet.
                      </td>
                    </tr>
                  ) : (
                    visibleEvents.map((ev) => (
                      <tr
                        key={ev.event_id}
                        onClick={() => navigate(`/logs?event=${encodeURIComponent(ev.event_id)}`)}
                        className={clsx(
                          "cursor-pointer transition-colors",
                          ev.severity === "CRITICAL"
                            ? "bg-[#fdecec] hover:bg-[#fbdada] text-[#b91c1c]"
                            : ev.severity === "WARN"
                              ? "bg-[#fffbeb] hover:bg-[#fef3c7] text-[#b45309]"
                              : "bg-white hover:bg-slate-50 text-[#0f172a]",
                        )}
                      >
                        <td className="px-3.5 py-2 whitespace-nowrap tabular opacity-80">{fmtClockUtc(ev.ts)}</td>
                        <td className="px-2 py-2 whitespace-nowrap">
                          <span
                            className={clsx(
                              "inline-block min-w-[66px] text-center rounded-[2px] border px-1.5 py-0.5 text-[10px] font-bold",
                              LEVEL_BADGE[ev.severity],
                            )}
                          >
                            {ev.severity}
                          </span>
                        </td>
                        <td className={clsx("px-2 py-2", ev.severity === "CRITICAL" && "font-bold")}>
                          {ev.description}
                        </td>
                        <td className="px-3.5 py-2 text-right whitespace-nowrap">
                          <span
                            className={clsx(
                              "inline-block min-w-[58px] text-center rounded-[2px] border px-2 py-0.5 text-[10px] font-semibold capitalize",
                              SOURCE_BADGE[ev.severity],
                            )}
                          >
                            {ev.source}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            <footer className="flex items-center justify-between border-t border-[#cbd5e1] bg-[#f8fafc] px-3.5 py-2 font-mono text-[11px]">
              <span className="text-slate-500 tabular">
                Showing {visibleEvents.length} of {fmtNumber(events.length)} events logged
              </span>
              <Link
                to="/logs"
                className="inline-flex items-center gap-1 font-bold text-[#1e3a8a] hover:underline"
              >
                View full logs in Event Logs tab <ArrowRight className="h-3 w-3" />
              </Link>
            </footer>
          </section>

          {/* Conveyor Control Panel */}
          <section className="rounded-[3px] border border-[#cbd5e1] bg-white p-4 shadow-card space-y-3.5">
            <h2 className="text-[15px] font-bold text-[#0f172a]">Conveyor Control Panel</h2>

            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={handlePrimaryAction}
                className="h-12 inline-flex items-center justify-center gap-2 rounded-[3px] border border-[#8a9cb3] bg-[#dde4ee] px-4 text-sm font-bold text-[#0f172a] hover:bg-[#d1dbe8] active:bg-[#c5d1e1] disabled:opacity-50"
              >
                <PrimaryIcon className="h-4 w-4" />
                <span>{primaryLabel}</span>
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void dispatchCommand("stop")}
                className="h-12 inline-flex items-center justify-center gap-2 rounded-[3px] bg-[#dc1e18] px-4 text-sm font-bold text-white hover:bg-[#b91c1c] active:bg-[#991b1b] disabled:opacity-50"
              >
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-full border-2 border-white">
                  <Square className="h-2 w-2 fill-current" />
                </span>
                <span>Stop</span>
              </button>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <div className="font-mono leading-tight">
                <div className="text-[11px] font-bold text-[#0f172a]">SPEED SETPOINT:</div>
                <div className="text-[10px] text-slate-500 tabular">(Max: {limits.max_mms} mm/s)</div>
              </div>

              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  aria-label="Decrease speed setpoint"
                  disabled={busy}
                  onClick={() => {
                    setSetpointTouched(true);
                    setSetpoint((s) => Math.max(limits.min_mms, s - limits.step_mms));
                  }}
                  className="h-10 w-10 inline-flex items-center justify-center rounded-[2px] border border-[#94a3b8] bg-[#e2e8f0] text-[#0f172a] hover:bg-[#cbd5e1] disabled:opacity-50"
                >
                  <Minus className="h-4 w-4" />
                </button>
                <div className="relative flex items-center">
                  <input
                    type="number"
                    aria-label="Speed setpoint in mm/s"
                    min={limits.min_mms}
                    max={limits.max_mms}
                    step={limits.step_mms}
                    value={setpoint}
                    onChange={(e) => {
                      setSetpointTouched(true);
                      setSetpoint(Number(e.target.value));
                    }}
                    className="h-10 w-24 rounded-[2px] border border-[#94a3b8] bg-white pl-2.5 pr-9 font-mono text-sm font-bold text-[#0f172a] tabular focus:border-[#1e3a8a] focus:outline-none"
                  />
                  <span className="pointer-events-none absolute right-2 font-mono text-[11px] text-slate-500">mm/s</span>
                </div>
                <button
                  type="button"
                  aria-label="Increase speed setpoint"
                  disabled={busy}
                  onClick={() => {
                    setSetpointTouched(true);
                    setSetpoint((s) => Math.min(limits.max_mms, s + limits.step_mms));
                  }}
                  className="h-10 w-10 inline-flex items-center justify-center rounded-[2px] border border-[#94a3b8] bg-[#e2e8f0] text-[#0f172a] hover:bg-[#cbd5e1] disabled:opacity-50"
                >
                  <Plus className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void dispatchCommand("set_speed", false, setpoint)}
                  className="h-10 inline-flex items-center justify-center gap-1.5 rounded-[2px] bg-[#1e3a8a] px-4 text-xs font-bold text-white hover:bg-[#172e6e] disabled:opacity-50"
                >
                  <Check className="h-3.5 w-3.5" />
                  <span>Apply</span>
                </button>
              </div>
            </div>

            {feedback && (
              <div
                className={clsx(
                  "rounded-[2px] border px-3 py-1.5 font-mono text-[11px]",
                  feedback.ok
                    ? "border-[#22c55e] bg-[#ecfdf3] text-[#15803d]"
                    : "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
                )}
              >
                {feedback.text}
              </div>
            )}
          </section>
        </div>
      </main>

      <ConfirmModal
        open={pendingConfirm !== null}
        title={pendingConfirm?.title ?? ""}
        description={pendingConfirm?.description ?? ""}
        confirmLabel={pendingConfirm?.confirmLabel ?? "Confirm"}
        telemetry={telemetry}
        busy={busy}
        onCancel={() => setPendingConfirm(null)}
        onConfirm={() => {
          if (!pendingConfirm) return;
          const act = pendingConfirm.action;
          const needsSetpoint = act === "start" || act === "resume";
          setPendingConfirm(null);
          void dispatchCommand(act, true, needsSetpoint ? setpoint : undefined);
        }}
      />
    </div>
  );
}
