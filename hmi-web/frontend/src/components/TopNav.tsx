import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { User } from "lucide-react";
import { clsx } from "clsx";
import { useHmi } from "../store/HmiContext";
import { FSM_LABEL, FSM_TONE, fmtDateTimeUtc, type Tone } from "../lib/format";

const TABS = [
  { to: "/", label: "Live Overview", end: true },
  { to: "/logs", label: "Event Logs", end: false },
  { to: "/diagnostics", label: "Diagnostics", end: false },
  { to: "/settings", label: "Settings", end: false },
];

const NAV_STATE_PILL: Record<Tone, { wrap: string; dot: string }> = {
  ok: { wrap: "border-emerald-500/75 bg-emerald-950/55 text-emerald-400", dot: "bg-emerald-400" },
  warn: { wrap: "border-amber-400/75 bg-amber-950/55 text-amber-300", dot: "bg-amber-400" },
  crit: { wrap: "border-red-500/80 bg-red-950/60 text-red-300", dot: "bg-red-400" },
  info: { wrap: "border-sky-400/75 bg-sky-950/55 text-sky-300", dot: "bg-sky-400" },
  muted: { wrap: "border-slate-500/60 bg-slate-900/60 text-slate-300", dot: "bg-slate-400" },
};

export function TopNav() {
  const { fsm, system } = useHmi();
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const stateKey = fsm?.current ?? "unknown";
  const pill = NAV_STATE_PILL[FSM_TONE[stateKey]];

  return (
    <header className="bg-[#0d253f] text-white select-none">
      <div className="mx-auto flex h-14 max-w-[1600px] items-stretch justify-between gap-4 px-5">
        {/* Left: Brand + FSM State Pill */}
        <div className="flex items-center gap-3.5 shrink-0">
          <span className="text-[15px] font-bold tracking-tight whitespace-nowrap">EAA HMI Console</span>
          <span className="h-4 w-px bg-[#284566]" aria-hidden="true" />
          <div
            className={clsx(
              "inline-flex items-center gap-2 rounded-[2px] border px-2.5 py-1 font-mono text-[11px] font-semibold uppercase tracking-wide tabular whitespace-nowrap",
              pill.wrap,
            )}
          >
            <span className={clsx("h-2 w-2 rounded-full", pill.dot, stateKey === "running" && "animate-pulse-dot")} />
            <span>SYSTEM STATE: {FSM_LABEL[stateKey]}</span>
          </div>
        </div>

        {/* Center: 4 Navigation Tabs (full height, active = solid white) */}
        <nav className="flex items-stretch shrink-0" aria-label="Primary">
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              end={tab.end}
              className={({ isActive }) =>
                clsx(
                  "flex items-center px-5 text-[14px] whitespace-nowrap transition-colors",
                  isActive
                    ? "bg-white text-[#0d253f] font-bold"
                    : "text-slate-300 font-medium hover:text-white hover:bg-[#153558]",
                )
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>

        {/* Right: UTC Timestamp Box + User Square Icon & Name */}
        <div className="flex items-center gap-3 shrink-0">
          <div className="hidden md:block rounded-[2px] border border-[#284566] bg-[#071628] px-2.5 py-1.5 font-mono text-[11px] text-slate-200 tabular whitespace-nowrap">
            {fmtDateTimeUtc(now)}
          </div>
          <div className="flex items-center gap-2 text-xs font-mono text-slate-100 whitespace-nowrap">
            <span className="inline-flex h-7 w-7 items-center justify-center rounded-[2px] bg-white text-[#0d253f]">
              <User className="h-4 w-4" />
            </span>
            <span className="hidden sm:inline">{system?.operator ?? "Operator"}</span>
          </div>
        </div>
      </div>
    </header>
  );
}
