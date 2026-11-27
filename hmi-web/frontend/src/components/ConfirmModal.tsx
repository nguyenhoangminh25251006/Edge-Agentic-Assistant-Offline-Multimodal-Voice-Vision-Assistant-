import { ShieldAlert, CheckCircle2, XCircle } from "lucide-react";
import type { TelemetrySnapshot } from "../api/types";
import { Badge } from "./Badge";

interface ConfirmModalProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  telemetry: TelemetrySnapshot | null;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmModal({
  open,
  title,
  description,
  confirmLabel,
  telemetry,
  busy = false,
  onCancel,
  onConfirm,
}: ConfirmModalProps) {
  if (!open) return null;

  const checks = [
    { label: "Hardware E-Stop Closed", ok: telemetry?.estop_closed ?? null },
    { label: "Photoelectric Beam Clear", ok: telemetry?.pe_clear ?? null },
    { label: "No Latched Hardware Fault", ok: telemetry ? !telemetry.fault_latched : null },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-navy-950/60 p-4" role="dialog" aria-modal="true">
      <div className="w-full max-w-md rounded border border-line bg-white shadow-xl">
        <div className="flex items-center gap-2.5 border-b border-line px-5 py-3.5 bg-panel">
          <ShieldAlert className="h-5 w-5 text-warn shrink-0" />
          <h3 className="text-base font-semibold text-ink">{title}</h3>
        </div>
        <div className="p-5 space-y-4">
          <p className="text-sm text-ink-muted">{description}</p>
          <div className="rounded border border-line bg-panel p-3 space-y-2">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-ink-faint">
              Pre-Motion Sensor Verification (FR-06)
            </div>
            {checks.map((c) => (
              <div key={c.label} className="flex items-center justify-between text-xs">
                <span className="text-ink">{c.label}</span>
                {c.ok === null ? (
                  <Badge tone="muted">UNKNOWN</Badge>
                ) : c.ok ? (
                  <span className="inline-flex items-center gap-1 font-semibold text-ok">
                    <CheckCircle2 className="h-3.5 w-3.5" /> PASS
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 font-semibold text-crit">
                    <XCircle className="h-3.5 w-3.5" /> BLOCKED
                  </span>
                )}
              </div>
            ))}
          </div>
          <p className="text-[11px] text-ink-faint">
            Note: The STM32 MCU Safety Supervisor remains final authority and will reject motion if any hardware interlock fails.
          </p>
        </div>
        <div className="flex items-center justify-end gap-3 border-t border-line px-5 py-3.5 bg-panel">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="min-h-[48px] min-w-[110px] rounded border border-line-strong bg-white px-4 text-sm font-semibold text-ink hover:bg-slate-50 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="min-h-[48px] min-w-[140px] rounded bg-accent px-5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {busy ? "Sending..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
