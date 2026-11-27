import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { ApiError, api } from "../api/client";
import { StationBar } from "../components/StationBar";
import { fmtAge, fmtClockUtc, fmtNumber } from "../lib/format";
import { useHmi } from "../store/HmiContext";

function sliderBackground(value: number, min = 50, max = 95): string {
  const pct = ((value - min) / (max - min)) * 100;
  return `linear-gradient(to right, #2563eb 0%, #2563eb ${pct}%, #cbd5e1 ${pct}%, #cbd5e1 100%)`;
}

export function SettingsPage() {
  const { settings, system } = useHmi();

  const [voicePct, setVoicePct] = useState(85);
  const [defectPct, setDefectPct] = useState(80);
  const [rollbackVer, setRollbackVer] = useState("");
  const [showNotes, setShowNotes] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ area: "update" | "cloud" | "cal"; ok: boolean; text: string } | null>(
    null,
  );

  useEffect(() => {
    if (!settings) return;
    setVoicePct(settings.voice_sensitivity_pct);
    setDefectPct(settings.defect_threshold_pct);
  }, [settings]);

  useEffect(() => {
    const versions = system?.updates.rollback_versions ?? [];
    if (versions.length > 0 && !rollbackVer) {
      setRollbackVer(versions[0]);
    }
  }, [system?.updates.rollback_versions, rollbackVer]);

  async function saveCalibration(nextVoice: number, nextDefect: number) {
    try {
      await api.updateSettings({
        voice_sensitivity_pct: nextVoice,
        defect_threshold_pct: nextDefect,
      });
    } catch (err) {
      setNotice({
        area: "cal",
        ok: false,
        text: err instanceof ApiError ? err.message : "Failed to save calibration",
      });
    }
  }

  async function runAction(
    area: "update" | "cloud" | "cal",
    fn: () => Promise<{ detail: string }>,
  ) {
    setBusy(true);
    setNotice(null);
    try {
      const res = await fn();
      setNotice({ area, ok: true, text: res.detail });
    } catch (err) {
      setNotice({
        area,
        ok: false,
        text: err instanceof ApiError ? err.message : "Request failed",
      });
    } finally {
      setBusy(false);
    }
  }

  if (!system) return null;

  const updates = system.updates;
  const cloud = system.cloud;
  const bounds = system.calibration_bounds;
  const cloudConnected = cloud.status === "connected";
  const rollbackOptions = updates.rollback_versions;

  return (
    <div className="min-h-[calc(100vh-56px)] flex flex-col">
      <StationBar />

      <main className="mx-auto w-full max-w-[1600px] flex-1 p-5 grid grid-cols-1 lg:grid-cols-2 gap-5 items-start">
        {/* Left Column: Update & Recovery + Cloud Sync */}
        <div className="flex flex-col gap-5">
          {/* UPDATE & RECOVERY */}
          <section className="rounded-[3px] border border-[#cbd5e1] bg-white p-5 shadow-card space-y-4">
            <header className="border-b border-[#e2e8f0] pb-3">
              <h2 className="font-mono text-sm font-bold uppercase tracking-wide text-[#0f172a]">
                UPDATE &amp; RECOVERY
              </h2>
              <p className="font-mono text-xs text-slate-500 mt-0.5">Firmware System Update &amp; Rollback</p>
            </header>

            {/* Current Version Box */}
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] px-4 py-3">
              <div className="font-mono text-xs">
                <strong className="text-[#0f172a]">
                  Current Version: {updates?.current_version ?? "None"}
                </strong>
                {updates?.build && <span className="ml-2 text-slate-500">(Build {updates.build})</span>}
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={() => void runAction("update", api.checkUpdates)}
                className="rounded-[2px] border border-[#94a3b8] bg-[#e2e8f0] px-3.5 py-2 font-mono text-[11px] font-bold uppercase text-[#0f172a] hover:bg-[#cbd5e1] disabled:opacity-50"
              >
                CHECK FOR UPDATES
              </button>
            </div>

            {/* Staged Update Box */}
            <div className="rounded-[2px] border border-[#f59e0b] bg-[#fffdf5] p-4 space-y-3">
              <div className="flex items-start justify-between gap-2">
                <div className="font-mono text-xs font-bold text-[#0f172a]">
                  {updates?.available
                    ? `Version ${updates.available.version} Available`
                    : "No Staged Update Available"}
                </div>
                <span className="rounded-[2px] border border-[#f59e0b] bg-[#fef3c7] px-2 py-0.5 font-mono text-[10px] font-bold uppercase text-[#b45309] shrink-0">
                  {updates?.available ? updates.available.status : "UP TO DATE"}
                </span>
              </div>

              <p className="font-mono text-[11px] text-slate-600 leading-relaxed">
                {updates?.available
                  ? `Changelog: ${updates.available.changelog}`
                  : updates?.note ?? "No Update Available"}
              </p>

              {showNotes && updates?.available?.release_notes && (
                <div className="rounded-[2px] border border-[#e2e8f0] bg-white p-2.5 font-mono text-[11px] text-slate-700">
                  {updates.available.release_notes}
                </div>
              )}

              <div className="flex flex-wrap items-center justify-end gap-2.5 pt-2 border-t border-[#fde68a]">
                <button
                  type="button"
                  disabled={!updates?.available}
                  onClick={() => setShowNotes((s) => !s)}
                  className="rounded-[2px] border border-[#b8c4d4] bg-white px-3.5 py-1.5 font-mono text-[11px] font-bold uppercase text-[#0f172a] hover:bg-slate-50 disabled:opacity-40"
                >
                  VIEW RELEASE NOTES
                </button>
                <button
                  type="button"
                  disabled={busy || !updates?.available}
                  onClick={() => void runAction("update", api.installUpdate)}
                  className="rounded-[2px] bg-[#16a34a] px-4 py-1.5 font-mono text-[11px] font-bold uppercase text-white hover:bg-[#15803d] disabled:opacity-40"
                >
                  INSTALL UPDATE
                </button>
              </div>
            </div>

            {/* Rollback Version Box */}
            <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-4 space-y-2.5">
              <label
                htmlFor="rollback-select"
                className="block font-mono text-[11px] font-bold uppercase text-[#0f172a]"
              >
                SELECT ROLLBACK VERSION:
              </label>
              <div className="flex flex-wrap items-center gap-3">
                <select
                  id="rollback-select"
                  value={rollbackVer}
                  onChange={(e) => setRollbackVer(e.target.value)}
                  disabled={rollbackOptions.length === 0}
                  className="h-9 flex-1 min-w-[200px] rounded-[2px] border border-[#b8c4d4] bg-white px-3 font-mono text-xs text-[#0f172a] focus:border-[#1e3a8a] focus:outline-none disabled:text-slate-400"
                >
                  {rollbackOptions.length === 0 ? (
                    <option value="">No rollback images available</option>
                  ) : (
                    rollbackOptions.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))
                  )}
                </select>
                <button
                  type="button"
                  disabled={busy || !rollbackVer}
                  onClick={() => void runAction("update", () => api.rollbackUpdate(rollbackVer))}
                  className="h-9 rounded-[2px] border border-[#d97706] bg-[#fef3c7] px-4 font-mono text-[11px] font-bold uppercase text-[#b45309] hover:bg-[#fde68a] disabled:opacity-40"
                >
                  ROLLBACK TO SELECTED VERSION
                </button>
              </div>
            </div>

            {notice?.area === "update" && (
              <div
                className={clsx(
                  "rounded-[2px] border px-3 py-1.5 font-mono text-[11px]",
                  notice.ok
                    ? "border-[#22c55e] bg-[#ecfdf3] text-[#15803d]"
                    : "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
                )}
              >
                {notice.text}
              </div>
            )}
          </section>

          {/* CLOUD SYNC */}
          <section className="rounded-[3px] border border-[#cbd5e1] bg-white p-5 shadow-card space-y-4">
            <header className="flex items-start justify-between gap-2 border-b border-[#e2e8f0] pb-3">
              <div>
                <h2 className="font-mono text-sm font-bold uppercase tracking-wide text-[#0f172a]">CLOUD SYNC</h2>
                <p className="font-mono text-xs text-slate-500 mt-0.5">AWS IoT Telemetry Ingest</p>
              </div>
              <span
                className={clsx(
                  "rounded-[2px] border px-2.5 py-0.5 font-mono text-[10px] font-bold uppercase",
                  cloudConnected
                    ? "border-[#22c55e] bg-[#ecfdf3] text-[#15803d]"
                    : "border-[#cbd5e1] bg-[#f8fafc] text-slate-500",
                )}
              >
                {cloudConnected ? "CONNECTED" : (cloud?.status ?? "OFFLINE").toUpperCase()}
              </span>
            </header>

            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3">
                <div className="font-mono text-[10px] font-semibold uppercase text-slate-500">QUEUE DEPTH</div>
                <div className="mt-1.5 font-mono text-xs font-bold text-[#0f172a] tabular">
                  {cloud?.queue_depth !== null && cloud?.queue_depth !== undefined
                    ? `${fmtNumber(cloud.queue_depth)} records`
                    : "-- records"}
                </div>
              </div>

              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3">
                <div className="font-mono text-[10px] font-semibold uppercase text-slate-500">BUFFER USAGE</div>
                <div className="mt-1.5 font-mono text-xs font-bold text-[#0f172a] tabular">
                  {cloud?.buffer_used_mb !== null && cloud?.buffer_used_mb !== undefined
                    ? `${fmtNumber(cloud.buffer_used_mb, 1)} MB / ${fmtNumber(cloud.buffer_limit_gb ?? 1.5, 1)} GB`
                    : "-- / -- GB"}
                </div>
              </div>

              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3">
                <div className="font-mono text-[10px] font-semibold uppercase text-slate-500">STATUS</div>
                <div className="mt-1.5 font-mono text-xs font-bold text-[#0f172a]">
                  {cloud?.sync_label ?? "Offline"}
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => void runAction("cloud", api.cloudSyncNow)}
                className="rounded-[2px] border border-[#94a3b8] bg-[#e2e8f0] px-4 py-2 font-mono text-[11px] font-bold uppercase text-[#0f172a] hover:bg-[#cbd5e1] disabled:opacity-50"
              >
                SYNC NOW
              </button>
              <span className="font-mono text-[11px] text-slate-500 tabular">
                Last Sync:{" "}
                {cloud?.last_sync_at ? `${fmtClockUtc(cloud.last_sync_at)} UTC (${fmtAge(cloud.last_sync_at)})` : "--"}
              </span>
            </div>

            {notice?.area === "cloud" && (
              <div
                className={clsx(
                  "rounded-[2px] border px-3 py-1.5 font-mono text-[11px]",
                  notice.ok
                    ? "border-[#22c55e] bg-[#ecfdf3] text-[#15803d]"
                    : "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
                )}
              >
                {notice.text}
              </div>
            )}
          </section>
        </div>

        {/* Right Column: AI PERCEPTION & VOICE CALIBRATION */}
        <section className="rounded-[3px] border border-[#cbd5e1] bg-white p-5 shadow-card space-y-4">
          <header className="border-b border-[#e2e8f0] pb-3">
            <h2 className="font-mono text-sm font-bold uppercase tracking-wide text-[#0f172a]">
              AI PERCEPTION &amp; VOICE CALIBRATION
            </h2>
            <p className="font-mono text-xs text-slate-500 mt-0.5">Detection Sensitivity &amp; Audio Feedback Tuning</p>
          </header>

          {/* Voice Command Sensitivity */}
          <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-4 space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-mono text-xs font-bold text-[#0f172a]">Voice Command Sensitivity</div>
                <div className="font-mono text-[11px] text-slate-500 mt-0.5">
                  Minimum confidence to trigger voice actions
                </div>
              </div>
              <span className="rounded-[2px] border border-[#94a3b8] bg-white px-2.5 py-1 font-mono text-xs font-bold text-[#0f172a] tabular">
                {voicePct}%
              </span>
            </div>

            <input
              type="range"
              aria-label="Voice Command Sensitivity"
              min={bounds.voice_min_pct}
              max={bounds.voice_max_pct}
              step={1}
              value={voicePct}
              style={{ background: sliderBackground(voicePct, bounds.voice_min_pct, bounds.voice_max_pct) }}
              onChange={(e) => setVoicePct(Number(e.target.value))}
              onMouseUp={() => void saveCalibration(voicePct, defectPct)}
              onTouchEnd={() => void saveCalibration(voicePct, defectPct)}
              onKeyUp={() => void saveCalibration(voicePct, defectPct)}
              className="hmi-slider w-full"
            />
            <div className="flex justify-between font-mono text-[10px] text-slate-500">
              <span>{bounds.voice_min_pct}%</span>
              <span>{bounds.voice_max_pct}%</span>
            </div>
          </div>

          {/* Defect Detection Threshold */}
          <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-4 space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-mono text-xs font-bold text-[#0f172a]">Defect Detection Threshold</div>
                <div className="font-mono text-[11px] text-slate-500 mt-0.5">
                  Minimum confidence score to flag a PCB defect
                </div>
              </div>
              <span className="rounded-[2px] border border-[#94a3b8] bg-white px-2.5 py-1 font-mono text-xs font-bold text-[#0f172a] tabular">
                {defectPct}%
              </span>
            </div>

            <input
              type="range"
              aria-label="Defect Detection Threshold"
              min={bounds.defect_min_pct}
              max={bounds.defect_max_pct}
              step={1}
              value={defectPct}
              style={{ background: sliderBackground(defectPct, bounds.defect_min_pct, bounds.defect_max_pct) }}
              onChange={(e) => setDefectPct(Number(e.target.value))}
              onMouseUp={() => void saveCalibration(voicePct, defectPct)}
              onTouchEnd={() => void saveCalibration(voicePct, defectPct)}
              onKeyUp={() => void saveCalibration(voicePct, defectPct)}
              className="hmi-slider w-full"
            />
            <div className="flex justify-between font-mono text-[10px] text-slate-500">
              <span>{bounds.defect_min_pct}%</span>
              <span>{bounds.defect_max_pct}%</span>
            </div>
          </div>

          {/* Bottom Divider + TEST MIC Button */}
          <div className="flex items-center justify-between gap-3 border-t border-[#e2e8f0] pt-4">
            <div className="font-mono text-[11px]">
              {notice?.area === "cal" && (
                <span className={notice.ok ? "text-[#15803d]" : "text-[#b91c1c]"}>{notice.text}</span>
              )}
            </div>
            <button
              type="button"
              disabled={busy}
              onClick={() => void runAction("cal", api.testMic)}
              className="rounded-[2px] border border-[#94a3b8] bg-[#e2e8f0] px-4 py-2 font-mono text-[11px] font-bold uppercase text-[#0f172a] hover:bg-[#cbd5e1] disabled:opacity-50"
            >
              TEST MIC
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}
