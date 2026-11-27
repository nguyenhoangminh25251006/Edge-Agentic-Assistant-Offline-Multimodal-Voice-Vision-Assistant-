import type { FsmState, SensorStatus, Severity } from "../api/types";

export type Tone = "ok" | "warn" | "crit" | "info" | "muted";

export function fmtNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "--";
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtClockUtc(ts: number | Date): string {
  const d = ts instanceof Date ? ts : new Date(ts * 1000);
  return d.toISOString().slice(11, 19);
}

export function fmtDateTimeUtc(ts: number | Date): string {
  const d = ts instanceof Date ? ts : new Date(ts * 1000);
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 19)} UTC`;
}

export function fmtAge(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

export function fmtUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "--";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export const FSM_LABEL: Record<FsmState, string> = {
  idle: "IDLE",
  running: "RUNNING",
  paused: "PAUSED",
  jammed: "JAMMED",
  safety_fault: "SAFETY FAULT",
  recovering: "RECOVERING",
  unknown: "UNKNOWN",
};

export const FSM_TONE: Record<FsmState, Tone> = {
  idle: "info",
  running: "ok",
  paused: "warn",
  jammed: "warn",
  safety_fault: "crit",
  recovering: "warn",
  unknown: "muted",
};

export const SEVERITY_TONE: Record<Severity, Tone> = { INFO: "ok", WARN: "warn", CRITICAL: "crit" };

export const SENSOR_LABEL: Record<SensorStatus, string> = {
  ok: "OK",
  clear: "CLEAR",
  blocked: "BLOCKED",
  estimated: "ESTIMATED",
  warn: "WARN",
  fault: "FAULT",
  offline: "OFFLINE",
};

export const SENSOR_TONE: Record<SensorStatus, Tone> = {
  ok: "ok",
  clear: "ok",
  blocked: "warn",
  estimated: "warn",
  warn: "warn",
  fault: "crit",
  offline: "muted",
};

/** Map a free-form hardware/IO state string to a tone. */
export function stateTone(state: string | null | undefined): Tone {
  const s = (state ?? "").toLowerCase();
  if (["normal", "ok", "safe", "clear", "armed", "closed", "running", "up", "nominal", "active"].includes(s)) return "ok";
  if (["warn", "warning", "estimated", "degraded", "stale", "sync", "blocked", "high"].includes(s)) return "warn";
  if (["fault", "error", "open", "tripped", "lost", "down", "critical", "stopped"].includes(s)) return "crit";
  return "muted";
}

export const TONE_CLASSES: Record<Tone, { text: string; bg: string; border: string; dot: string }> = {
  ok: { text: "text-ok", bg: "bg-ok-soft", border: "border-ok-line", dot: "bg-ok-bright" },
  warn: { text: "text-warn", bg: "bg-warn-soft", border: "border-warn-line", dot: "bg-warn-bright" },
  crit: { text: "text-crit", bg: "bg-crit-soft", border: "border-crit-line", dot: "bg-crit-bright" },
  info: { text: "text-accent", bg: "bg-accent-soft", border: "border-accent-line", dot: "bg-accent" },
  muted: { text: "text-ink-faint", bg: "bg-panel", border: "border-line", dot: "bg-slate-400" },
};

export function pct(used: number | null | undefined, total: number | null | undefined): number | null {
  if (used === null || used === undefined || !total) return null;
  return Math.max(0, Math.min(100, (used / total) * 100));
}
