import { ArrowLeftRight, BarChart3, Cpu, HeartPulse, LayoutGrid, Network, Server } from "lucide-react";
import { clsx } from "clsx";
import type { ContainerHealth, ContainerStatus, LinkState, McuIo, PowerRail } from "../api/types";
import { StationBar } from "../components/StationBar";
import { fmtNumber, fmtUptime, pct, stateTone, type Tone } from "../lib/format";
import { useHmi } from "../store/HmiContext";

const LINK_LABEL: Record<LinkState, string> = {
  up: "ACTIVE & SYNCHRONIZED",
  sync: "HANDSHAKING",
  stale: "DEGRADED (STALE)",
  lost: "LINK LOST",
  down: "DISCONNECTED",
};

const STATUS_BADGE: Record<Tone, string> = {
  ok: "border-[#22c55e] bg-[#ecfdf3] text-[#15803d]",
  warn: "border-[#f59e0b] bg-[#fffbeb] text-[#b45309]",
  crit: "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
  info: "border-[#3b82f6] bg-[#eff6ff] text-[#1d4ed8]",
  muted: "border-[#cbd5e1] bg-[#f8fafc] text-slate-500",
};

const CONTAINER_TONE: Record<ContainerStatus, Tone> = {
  running: "ok",
  degraded: "warn",
  stopped: "muted",
  disabled: "muted",
};

function fmtBufferCap(bytes: number): string {
  return bytes >= 1024 ? `${Math.round(bytes / 1024)} KB` : `${bytes} B`;
}

export function DiagnosticsPage() {
  const { diagnostics, system } = useHmi();
  if (!system) return null;

  const defs = system.diagnostics_defaults;
  const host = diagnostics?.host;
  const bridge = diagnostics?.bridge;
  const mcu = diagnostics?.mcu;

  const containers: ContainerHealth[] =
    host && host.containers.length > 0
      ? host.containers
      : defs.containers.map((name) => ({
          name,
          status: name === "container-web" ? "running" : "stopped",
          cpu_pct: null,
          ram_mb: null,
          ram_limit_mb: null,
          uptime_s: null,
        }));
  const mcuIo: McuIo[] =
    mcu && mcu.io.length > 0
      ? mcu.io
      : defs.mcu_io.map((r) => ({ ...r, value: r.value ?? null, state: r.state || "OFFLINE" }));
  const powerRails: PowerRail[] =
    mcu && mcu.power.length > 0
      ? mcu.power
      : defs.power_rails.map((p) => ({ ...p, measured_v: p.measured_v ?? null }));

  const ramPct = pct(host?.ram_used_mb, host?.ram_total_mb ?? defs.ram_total_mb);
  const storagePct = pct(host?.storage_used_gb, host?.storage_total_gb ?? defs.storage_total_gb);
  const txCap = bridge?.tx_buffer_size ?? defs.buffer_size_bytes;
  const rxCap = bridge?.rx_buffer_size ?? defs.buffer_size_bytes;
  const txPct = pct(bridge?.tx_buffer_used, txCap);
  const rxPct = pct(bridge?.rx_buffer_used, rxCap);

  const histBins =
    bridge && bridge.histogram.length > 0
      ? bridge.histogram
      : defs.histogram_bins.map((label) => ({ label, count: 0 }));
  const maxBin = Math.max(1, ...histBins.map((b) => b.count));

  const linkTone: Tone = bridge ? stateTone(bridge.link_state) : "muted";
  const bridgeUp = bridge?.link_state === "up";
  const watchdogArmed = bridge?.link_state === "up" || bridge?.link_state === "stale";

  return (
    <div className="min-h-[calc(100vh-56px)] flex flex-col">
      <StationBar />

      <main className="mx-auto w-full max-w-[1600px] flex-1 p-4 grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
        {/* Column 1: Edge Host MPU & Container Services */}
        <section className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card">
          <header className="flex items-start gap-2.5 border-b border-[#e2e8f0] px-4 py-3.5">
            <Server className="h-5 w-5 text-[#0d253f] mt-0.5 shrink-0" />
            <div>
              <h2 className="text-[15px] font-bold text-[#0f172a] leading-tight">
                Edge Host MPU &amp; Container Services
              </h2>
              <p className="font-mono text-[11px] text-slate-500 mt-0.5">
                {host?.model ?? defs?.host_model ?? "Qualcomm QRB2210"}
              </p>
            </div>
          </header>

          <div className="p-4 space-y-3">
            {/* CPU / RAM / STORAGE Summary Box */}
            <div className="grid grid-cols-3 gap-3 rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 text-center">
              <div>
                <div className="font-mono text-[10px] font-semibold uppercase text-slate-500">CPU</div>
                <div className="mt-1 font-mono text-sm font-bold text-[#0f172a] tabular">
                  {host?.cpu_pct !== null && host?.cpu_pct !== undefined ? `${fmtNumber(host.cpu_pct, 0)}%` : "--%"}
                </div>
                <div className="mt-2 h-1.5 w-full rounded-full bg-[#dce4ee] overflow-hidden">
                  <div className="h-full bg-[#2563eb]" style={{ width: `${Math.min(100, host?.cpu_pct ?? 0)}%` }} />
                </div>
              </div>

              <div>
                <div className="font-mono text-[10px] font-semibold uppercase text-slate-500">RAM</div>
                <div className="mt-1 font-mono text-sm font-bold text-[#0f172a] tabular">
                  {host?.ram_used_mb !== null && host?.ram_used_mb !== undefined
                    ? `${(host.ram_used_mb / 1024).toFixed(1)} GB`
                    : "-- GB"}
                  {ramPct !== null && (
                    <span className="ml-1 text-[10px] font-normal text-slate-500">{ramPct.toFixed(0)}%</span>
                  )}
                </div>
                <div className="mt-2 h-1.5 w-full rounded-full bg-[#dce4ee] overflow-hidden">
                  <div className="h-full bg-[#2563eb]" style={{ width: `${ramPct ?? 0}%` }} />
                </div>
              </div>

              <div>
                <div className="font-mono text-[10px] font-semibold uppercase text-slate-500">STORAGE</div>
                <div className="mt-1 font-mono text-sm font-bold text-[#0f172a] tabular">
                  {host?.storage_used_gb !== null && host?.storage_used_gb !== undefined
                    ? `${fmtNumber(host.storage_used_gb, 1)} GB`
                    : "-- GB"}
                  {storagePct !== null && (
                    <span className="ml-1 text-[10px] font-normal text-slate-500">{storagePct.toFixed(0)}%</span>
                  )}
                </div>
                <div className="mt-2 h-1.5 w-full rounded-full bg-[#dce4ee] overflow-hidden">
                  <div className="h-full bg-[#2563eb]" style={{ width: `${storagePct ?? 0}%` }} />
                </div>
              </div>
            </div>

            {/* Container Rows */}
            <div className="space-y-2.5">
              {containers.length === 0 ? (
                <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] px-4 py-8 text-center font-mono text-[11px] text-slate-400">
                  No containers reported yet.
                </div>
              ) : (
                containers.map((c) => {
                  const tone = CONTAINER_TONE[c.status];
                  return (
                    <div key={c.name} className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-2">
                      <div className="flex items-center justify-between gap-2">
                        <span className="inline-flex items-center gap-2 font-mono text-xs font-bold text-[#0f172a]">
                          <span
                            className={clsx(
                              "h-2 w-2 rounded-full",
                              c.status === "running"
                                ? "bg-[#16a34a]"
                                : c.status === "degraded"
                                  ? "bg-[#f59e0b]"
                                  : "bg-slate-400",
                            )}
                          />
                          {c.name}
                        </span>
                        <span
                          className={clsx(
                            "rounded-[2px] border px-2 py-0.5 font-mono text-[10px] font-bold uppercase",
                            STATUS_BADGE[tone],
                          )}
                        >
                          {c.status}
                        </span>
                      </div>
                      <div className="flex items-center justify-between font-mono text-[11px] text-slate-500 tabular">
                        <span>
                          CPU: <strong className="text-[#0f172a]">{c.cpu_pct !== null ? `${c.cpu_pct.toFixed(0)}%` : "--%"}</strong>
                        </span>
                        <span>
                          RAM:{" "}
                          <strong className="text-[#0f172a]">
                            {c.ram_mb !== null ? `${fmtNumber(c.ram_mb, 0)} MB` : "-- MB"}
                          </strong>
                        </span>
                        <span>Up {fmtUptime(c.uptime_s)}</span>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </section>

        {/* Column 2: Inter-Processor Communication (IPC Bridge) */}
        <section className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card">
          <header className="flex items-start gap-2.5 border-b border-[#e2e8f0] px-4 py-3.5">
            <Network className="h-5 w-5 text-[#0d253f] mt-0.5 shrink-0" />
            <div>
              <h2 className="text-[15px] font-bold text-[#0f172a] leading-tight">
                Inter-Processor Communication (IPC Bridge)
              </h2>
              <p className="font-mono text-[11px] text-slate-500 mt-0.5">
                {bridge?.transport ?? defs?.bridge_transport ?? "Arduino Bridge RPC"}
              </p>
            </div>
          </header>

          <div className="p-4 space-y-3">
            {/* Link Status Banner */}
            <div
              className={clsx(
                "flex items-center gap-2 rounded-[2px] border px-3 py-2 font-mono text-[11px] font-bold uppercase",
                STATUS_BADGE[linkTone],
              )}
            >
              <span
                className={clsx(
                  "h-2 w-2 rounded-full",
                  linkTone === "ok"
                    ? "bg-[#16a34a]"
                    : linkTone === "warn"
                      ? "bg-[#f59e0b]"
                      : linkTone === "crit"
                        ? "bg-[#dc2626]"
                        : "bg-slate-400",
                )}
              />
              <span>{bridge ? LINK_LABEL[bridge.link_state] : "DISCONNECTED"}</span>
            </div>

            {/* 2x2 KPI Tiles */}
            <div className="grid grid-cols-2 gap-2.5">
              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-2">
                <div className="flex items-center justify-between font-mono text-[11px] text-slate-500">
                  <span>Heartbeat</span>
                  <HeartPulse className="h-4 w-4 text-[#15803d]" />
                </div>
                <div className="flex items-baseline justify-between font-mono tabular">
                  <span className="text-sm font-bold text-[#0f172a]">
                    {bridge?.heartbeat_period_ms !== null && bridge?.heartbeat_period_ms !== undefined
                      ? `${fmtNumber(bridge.heartbeat_period_ms, 0)} ms`
                      : "-- ms"}
                  </span>
                  <span className="text-[10px] font-semibold text-[#15803d]">
                    {defs?.heartbeat_base_label ?? "200 Hz Base"}
                  </span>
                </div>
              </div>

              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-2">
                <div className="flex items-center justify-between font-mono text-[11px] text-slate-500">
                  <span>Round-Trip Latency</span>
                  <span className="text-[10px] font-bold text-[#15803d]">
                    {bridge?.rtt_ms !== null &&
                    bridge?.rtt_ms !== undefined &&
                    bridge.rtt_ms <= (defs?.rtt_limit_ms ?? 15)
                      ? "NOMINAL"
                      : "--"}
                  </span>
                </div>
                <div className="flex items-baseline justify-between font-mono tabular">
                  <span className="text-sm font-bold text-[#0f172a]">
                    {bridge?.rtt_ms !== null && bridge?.rtt_ms !== undefined ? `${bridge.rtt_ms.toFixed(1)} ms` : "-- ms"}
                  </span>
                  <span className="text-[10px] text-slate-500">≤ {(defs?.rtt_limit_ms ?? 15).toFixed(1)}ms</span>
                </div>
              </div>

              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-2">
                <div className="flex items-center justify-between font-mono text-[11px] text-slate-500">
                  <span>Command TTL</span>
                  <span className={clsx("text-[10px] font-bold", bridgeUp ? "text-[#15803d]" : "text-slate-400")}>
                    {bridgeUp ? "ACTIVE" : "--"}
                  </span>
                </div>
                <div className="flex items-baseline justify-between font-mono tabular">
                  <span className="text-sm font-bold text-[#0f172a]">
                    {bridge?.command_ttl_ms !== null && bridge?.command_ttl_ms !== undefined
                      ? `${fmtNumber(bridge.command_ttl_ms, 0)} ms`
                      : `${defs.command_ttl_ms} ms`}
                  </span>
                  <span className="text-[10px] text-slate-500">≤ {defs.command_ttl_ms} ms Window</span>
                </div>
              </div>

              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-2">
                <div className="flex items-center justify-between font-mono text-[11px] text-slate-500">
                  <span>Link-Loss Timeout</span>
                  <span className={clsx("text-[10px] font-bold", watchdogArmed ? "text-[#15803d]" : "text-slate-400")}>
                    {watchdogArmed ? "ARMED" : "--"}
                  </span>
                </div>
                <div className="flex items-baseline justify-between font-mono tabular">
                  <span className="text-sm font-bold text-[#0f172a]">
                    {bridge?.link_loss_timeout_ms !== null && bridge?.link_loss_timeout_ms !== undefined
                      ? `${fmtNumber(bridge.link_loss_timeout_ms, 0)} ms`
                      : `${defs.link_loss_timeout_ms} ms`}
                  </span>
                  <span className="text-[10px] text-slate-500">Max: {defs.link_loss_timeout_ms} ms</span>
                </div>
              </div>
            </div>

            {/* Bridge Latency Distribution */}
            <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-3">
              <div className="flex items-start justify-between gap-2 font-mono text-[11px]">
                <div className="flex items-start gap-1.5">
                  <BarChart3 className="h-4 w-4 text-[#2563eb] mt-0.5 shrink-0" />
                  <div>
                    <strong className="text-[#0f172a]">Bridge Latency Distribution</strong>{" "}
                    <span className="text-slate-500">({defs.histogram_window_label})</span>
                  </div>
                </div>
                <div className="text-right text-[10px] font-bold text-[#2563eb] tabular shrink-0">
                  <div>
                    μ=
                    {bridge?.rtt_mean_ms !== null && bridge?.rtt_mean_ms !== undefined
                      ? `${bridge.rtt_mean_ms.toFixed(1)}ms`
                      : "--"}
                  </div>
                  <div>
                    σ=
                    {bridge?.rtt_std_ms !== null && bridge?.rtt_std_ms !== undefined
                      ? `${bridge.rtt_std_ms.toFixed(1)}ms`
                      : "--"}
                  </div>
                </div>
              </div>

              <div className="flex items-end gap-1.5 h-24 pt-2 border-b border-dashed border-[#cbd5e1] pb-1">
                {histBins.map((bin, idx) => {
                  const pctH = bin.count > 0 ? Math.max(8, (bin.count / maxBin) * 100) : 4;
                  const barColor =
                    bin.count === 0
                      ? "bg-slate-200"
                      : idx >= histBins.length - 1
                        ? "bg-[#dc2626]"
                        : idx === histBins.length - 2
                          ? "bg-[#d97706]"
                          : idx === 3
                            ? "bg-[#2563eb]"
                            : "bg-[#60a5fa]";
                  return (
                    <div key={bin.label} className="flex-1 flex flex-col items-center justify-end h-full gap-1">
                      <div
                        title={`${bin.label}: ${bin.count}`}
                        style={{ height: `${pctH}%` }}
                        className={clsx("w-full rounded-t-[1px]", barColor)}
                      />
                    </div>
                  );
                })}
              </div>
              <div className="flex justify-between font-mono text-[9px] text-slate-500 tabular">
                {histBins.map((b) => (
                  <span key={b.label}>{b.label}</span>
                ))}
              </div>

              <div className="flex items-center justify-between font-mono text-[10px] text-slate-500 tabular pt-1">
                <span>Frames: {fmtNumber(bridge?.frames_total)}</span>
                <span>CRC Passes: {fmtNumber(bridge?.crc_pass)}</span>
                <span>Dropped: {fmtNumber(bridge?.dropped)}</span>
              </div>
            </div>

            {/* Inter-Brain Bridge Topography */}
            <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3 space-y-3">
              <div className="font-mono text-[10px] font-bold uppercase tracking-wider text-slate-600">
                INTER-BRAIN BRIDGE TOPOGRAPHY
              </div>
              <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
                <div className="rounded-[2px] border border-[#cbd5e1] bg-white p-2.5 text-center space-y-1">
                  <Cpu className="h-4 w-4 mx-auto text-[#0d253f]" />
                  <div className="font-mono text-[11px] font-bold text-[#0f172a] leading-tight">
                    Application Host (MPU)
                  </div>
                  <div className="font-mono text-[10px] text-[#15803d]">[Command Sender]</div>
                </div>

                <div className="flex flex-col items-center gap-1">
                  <ArrowLeftRight className="h-4 w-4 text-[#2563eb]" />
                  <span className="rounded-[2px] border border-[#cbd5e1] bg-white px-2 py-0.5 font-mono text-[10px] font-bold text-[#0f172a]">
                    Bridge RPC
                  </span>
                  <span className="font-mono text-[9px] text-slate-500 tabular">
                    Out: {txPct !== null ? `${txPct.toFixed(0)}%` : "--%"} | In:{" "}
                    {rxPct !== null ? `${rxPct.toFixed(0)}%` : "--%"}
                  </span>
                </div>

                <div className="rounded-[2px] border border-[#cbd5e1] bg-white p-2.5 text-center space-y-1">
                  <LayoutGrid className="h-4 w-4 mx-auto text-[#0d253f]" />
                  <div className="font-mono text-[11px] font-bold text-[#0f172a] leading-tight">
                    Real-Time Core (MCU)
                  </div>
                  <div className="font-mono text-[10px] text-[#15803d]">[Telemetry Receiver]</div>
                </div>
              </div>

              <div className="space-y-2 font-mono text-[10px]">
                <div>
                  <div className="flex justify-between text-slate-600 tabular mb-1">
                    <span>Outbound Commands Buffer ({fmtBufferCap(txCap)}):</span>
                    <strong className="text-[#0f172a]">
                      {fmtNumber(bridge?.tx_buffer_used)} B ({txPct !== null ? `${txPct.toFixed(0)}%` : "--%"})
                    </strong>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-[#dce4ee] overflow-hidden">
                    <div className="h-full bg-[#2563eb]" style={{ width: `${txPct ?? 0}%` }} />
                  </div>
                </div>
                <div>
                  <div className="flex justify-between text-slate-600 tabular mb-1">
                    <span>Inbound Telemetry Buffer ({fmtBufferCap(rxCap)}):</span>
                    <strong className="text-[#0f172a]">
                      {fmtNumber(bridge?.rx_buffer_used)} B ({rxPct !== null ? `${rxPct.toFixed(0)}%` : "--%"})
                    </strong>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-[#dce4ee] overflow-hidden">
                    <div className="h-full bg-[#2563eb]" style={{ width: `${rxPct ?? 0}%` }} />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Column 3: Real-Time MCU & Hardware I/O */}
        <section className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card overflow-hidden">
          <header className="flex items-start gap-2.5 border-b border-[#e2e8f0] px-4 py-3.5">
            <LayoutGrid className="h-5 w-5 text-[#0d253f] mt-0.5 shrink-0" />
            <div>
              <h2 className="text-[15px] font-bold text-[#0f172a] leading-tight">Real-Time MCU &amp; Hardware I/O</h2>
              <p className="font-mono text-[11px] text-slate-500 mt-0.5">
                {mcu?.model ?? defs?.mcu_model ?? "STM32U585"}
              </p>
            </div>
          </header>

          <div className="overflow-x-auto">
            <table className="w-full text-left font-mono text-[11px]">
              <thead className="border-b border-[#cbd5e1] bg-[#eef2f6] text-[10px] font-bold uppercase tracking-wider text-slate-600">
                <tr>
                  <th className="px-4 py-2.5">CHANNEL / PORT</th>
                  <th className="px-2 py-2.5">FUNCTION &amp; VALUE</th>
                  <th className="px-4 py-2.5 text-right">STATE</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#e2e8f0]">
                {mcuIo.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="px-4 py-10 text-center text-slate-400">
                      No MCU I/O channels reported yet.
                    </td>
                  </tr>
                ) : (
                  mcuIo.map((row) => {
                    const tone = stateTone(row.state);
                    return (
                      <tr key={`${row.port}-${row.function}`}>
                        <td className="px-4 py-2.5 font-bold text-[#0f172a] whitespace-nowrap">{row.port}</td>
                        <td className="px-2 py-2.5 text-slate-700">
                          {row.function}
                          {" • "}
                          <span className={clsx("font-bold", tone === "ok" ? "text-[#15803d]" : "text-slate-500")}>
                            {row.value ?? "--"}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-right whitespace-nowrap">
                          <span
                            className={clsx(
                              "inline-block min-w-[60px] text-center rounded-[2px] border px-2 py-0.5 text-[10px] font-bold uppercase",
                              STATUS_BADGE[tone],
                            )}
                          >
                            {row.state}
                          </span>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          <div className="border-t border-[#cbd5e1] p-4 space-y-2.5">
            <div className="font-mono text-[10px] font-bold uppercase tracking-wider text-slate-600">
              HARDWARE POWER MONITORS
            </div>
            <div className="divide-y divide-[#e2e8f0]">
              {powerRails.map((rail) => {
                const dev =
                  rail.measured_v !== null ? Math.abs(rail.measured_v - rail.nominal_v) / rail.nominal_v : 0;
                const railTone: Tone = rail.measured_v === null ? "muted" : dev > 0.1 ? "warn" : "ok";
                return (
                  <div key={rail.name} className="flex items-center justify-between py-2.5 first:pt-1 last:pb-0">
                    <div>
                      <div className="font-mono text-xs font-bold text-[#0f172a]">{rail.name}</div>
                      <div className="font-mono text-[10px] text-slate-500">
                        Nominal: {rail.nominal_v.toFixed(1)}V
                      </div>
                    </div>
                    <span
                      className={clsx(
                        "min-w-[68px] text-center rounded-[2px] border px-2.5 py-1 font-mono text-xs font-bold tabular",
                        STATUS_BADGE[railTone],
                      )}
                    >
                      {rail.measured_v !== null ? `${rail.measured_v.toFixed(2)} V` : "-- V"}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
