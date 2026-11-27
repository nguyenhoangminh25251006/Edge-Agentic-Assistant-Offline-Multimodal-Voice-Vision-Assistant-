import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Check, Copy, Download, Search, X } from "lucide-react";
import { clsx } from "clsx";
import { api } from "../api/client";
import type { LogEntry, LogFilters, LogPage, Severity } from "../api/types";
import { StationBar } from "../components/StationBar";
import { fmtClockUtc, fmtNumber } from "../lib/format";
import { useHmi } from "../store/HmiContext";

const PAGE_SIZE = 19;

const LEVEL_BADGE: Record<Severity, string> = {
  INFO: "border-[#94a3b8] bg-[#f8fafc] text-[#334155]",
  WARN: "border-[#f59e0b] bg-[#fffbeb] text-[#b45309]",
  CRITICAL: "border-[#ef4444] bg-[#fef2f2] text-[#b91c1c]",
};

function pageRange(current: number, total: number): Array<number | "..."> {
  if (total <= 5) return Array.from({ length: total }, (_, i) => i + 1);
  if (current <= 3) return [1, 2, 3, "...", total];
  if (current >= total - 2) return [1, "...", total - 2, total - 1, total];
  return [1, "...", current, "...", total];
}

export function EventLogsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedId = searchParams.get("event");
  const { events: liveEvents, snapshotsReady } = useHmi();

  const [searchInput, setSearchInput] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [page, setPage] = useState(1);

  const [data, setData] = useState<LogPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<LogEntry | null>(null);
  const [drawerClosed, setDrawerClosed] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebouncedSearch(searchInput.trim());
      setPage(1);
    }, 250);
    return () => window.clearTimeout(t);
  }, [searchInput]);

  const filters: LogFilters = useMemo(
    () => ({
      page,
      pageSize: PAGE_SIZE,
      severity: [],
      source: "",
      search: debouncedSearch,
    }),
    [page, debouncedSearch],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .logs(filters)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        setSelected((prev) => {
          if (requestedId) {
            const found = res.items.find((i) => i.id === requestedId);
            if (found) return found;
          }
          if (prev && res.items.some((i) => i.id === prev.id)) return prev;
          return res.items[0] ?? null;
        });
      })
      .catch(() => {
        if (!cancelled) setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [filters, requestedId, liveEvents.length]);

  useEffect(() => {
    if (!requestedId) return;
    setDrawerClosed(false);
    api
      .log(requestedId)
      .then((entry) => setSelected(entry))
      .catch(() => {});
  }, [requestedId]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const pages = pageRange(page, totalPages);

  async function handleCopyJson() {
    if (!selected) return;
    const raw = JSON.stringify(selected.payload ?? selected, null, 2);
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  }

  const t = selected?.telemetry ?? {};
  const hasSnapshot = selected ? selected.has_snapshot || Boolean(snapshotsReady[selected.id]) : false;

  return (
    <div className="min-h-[calc(100vh-56px)] flex flex-col">
      <StationBar
        left={
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
            <input
              type="search"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search events, PCB IDs..."
              aria-label="Search event logs"
              className="h-[34px] w-60 sm:w-72 rounded-[2px] border border-[#b8c4d4] bg-white pl-8 pr-3 font-mono text-xs text-slate-800 placeholder:text-slate-400 focus:border-[#1e3a8a] focus:outline-none"
            />
          </div>
        }
        right={
          <a
            href={api.logsCsvUrl(filters)}
            download
            className="inline-flex h-[34px] items-center gap-1.5 rounded-[2px] border border-[#b8c4d4] bg-[#eef2f6] px-3.5 font-mono text-xs font-semibold text-slate-800 hover:bg-[#e2e8f0]"
          >
            <Download className="h-3.5 w-3.5" />
            <span>Export CSV</span>
          </a>
        }
      />

      <main
        className={clsx(
          "mx-auto w-full max-w-[1600px] flex-1 p-4 grid grid-cols-1 gap-4 items-start",
          !drawerClosed && "xl:grid-cols-[1.85fr_1fr]",
        )}
      >
        {/* Left: Paginated Log Table */}
        <section className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left font-mono text-xs">
              <thead className="border-b border-[#cbd5e1] bg-[#e9eff6] text-[10px] font-bold uppercase tracking-wider text-slate-700">
                <tr>
                  <th className="px-4 py-3">TIMESTAMP</th>
                  <th className="px-3 py-3 text-center">LEVEL</th>
                  <th className="px-3 py-3">DESCRIPTION</th>
                  <th className="px-4 py-3 text-right">SOURCE</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#e2e8f0]">
                {loading && !data ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-14 text-center text-slate-400">
                      Loading events...
                    </td>
                  </tr>
                ) : !data || data.items.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-14 text-center text-slate-400">
                      No events recorded yet.
                    </td>
                  </tr>
                ) : (
                  data.items.map((item) => {
                    const isSelected = selected?.id === item.id;
                    const isCrit = item.severity === "CRITICAL";
                    return (
                      <tr
                        key={item.id}
                        onClick={() => {
                          setSelected(item);
                          setDrawerClosed(false);
                          setSearchParams({ event: item.id });
                        }}
                        className={clsx(
                          "cursor-pointer transition-colors",
                          isSelected
                            ? isCrit
                              ? "bg-[#fdecec] text-[#b91c1c] border-l-4 border-l-[#dc2626]"
                              : "bg-[#eff6ff] text-[#0f172a] border-l-4 border-l-[#1e3a8a]"
                            : "hover:bg-slate-50 text-[#0f172a]",
                        )}
                      >
                        <td className="px-4 py-2.5 whitespace-nowrap tabular opacity-85">{fmtClockUtc(item.ts)}</td>
                        <td className="px-3 py-2.5 text-center whitespace-nowrap">
                          <span
                            className={clsx(
                              "inline-block min-w-[70px] rounded-[2px] border px-2 py-0.5 text-[10px] font-bold",
                              LEVEL_BADGE[item.severity],
                            )}
                          >
                            {item.severity}
                          </span>
                        </td>
                        <td className={clsx("px-3 py-2.5", isSelected && isCrit && "font-bold")}>
                          {item.description}
                        </td>
                        <td className="px-4 py-2.5 text-right whitespace-nowrap opacity-85">{item.source}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-[#cbd5e1] bg-[#f8fafc] px-4 py-2.5 font-mono text-[11px]">
            <span className="text-slate-600 tabular">
              Showing <strong className="text-slate-900">{data?.items.length ?? 0}</strong> of{" "}
              <strong className="text-slate-900">{fmtNumber(data?.total ?? 0)}</strong> events logged
            </span>
            <div className="flex items-center gap-1 tabular">
              <button
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded-[2px] border border-[#cbd5e1] bg-white px-2.5 py-1 text-slate-700 hover:bg-slate-100 disabled:opacity-40"
              >
                &lt; Prev
              </button>
              {pages.map((p, idx) =>
                p === "..." ? (
                  <span key={`e-${idx}`} className="px-1.5 text-slate-400">
                    ...
                  </span>
                ) : (
                  <button
                    key={p}
                    type="button"
                    onClick={() => setPage(p)}
                    className={clsx(
                      "min-w-[28px] rounded-[2px] border px-2 py-1 font-bold",
                      p === page
                        ? "border-[#0d253f] bg-[#0d253f] text-white"
                        : "border-[#cbd5e1] bg-white text-slate-700 hover:bg-slate-100",
                    )}
                  >
                    {p}
                  </button>
                ),
              )}
              <button
                type="button"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="rounded-[2px] border border-[#cbd5e1] bg-white px-2.5 py-1 text-slate-700 hover:bg-slate-100 disabled:opacity-40"
              >
                Next &gt;
              </button>
            </div>
          </footer>
        </section>

        {/* Right: Event Details Drawer */}
        {!drawerClosed && (
          <aside className="rounded-[3px] border border-[#cbd5e1] bg-white shadow-card overflow-hidden">
            <header className="flex items-center justify-between gap-2 border-b border-[#cbd5e1] px-4 py-3">
              <div className="flex items-center gap-2 min-w-0">
                <h2 className="text-[15px] font-bold text-[#0f172a]">Event Details</h2>
                <span className="font-mono text-[11px] text-slate-500 tabular truncate">
                  [ {selected ? `${fmtClockUtc(selected.ts)} UTC` : "--:--:-- UTC"} ]
                </span>
                {selected && (
                  <span
                    className={clsx(
                      "rounded-[2px] border px-2 py-0.5 font-mono text-[10px] font-bold",
                      LEVEL_BADGE[selected.severity],
                    )}
                  >
                    {selected.severity}
                  </span>
                )}
              </div>
              <button
                type="button"
                aria-label="Close event details"
                onClick={() => {
                  setDrawerClosed(true);
                  setSearchParams({});
                }}
                className="rounded p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-900"
              >
                <X className="h-4 w-4" />
              </button>
            </header>

            <div className="p-4 space-y-4">
              {/* Metadata & Sensor Snapshot Box */}
              <div className="rounded-[2px] border border-[#cbd5e1] bg-[#f8fafc] p-3.5 font-mono text-[11px] space-y-1.5">
                <div className="flex items-start justify-between gap-3">
                  <span className="text-slate-500 shrink-0">Description:</span>
                  <span className="font-bold text-[#0f172a] text-right">{selected?.description ?? "--"}</span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-slate-500">Station:</span>
                  <span className="font-bold text-[#0f172a]">{selected?.station_id ?? "--"}</span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-slate-500">Source:</span>
                  <span className="font-bold text-[#0f172a]">{selected?.source ?? "--"}</span>
                </div>

                <div className="pt-2 space-y-1.5">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Speed:</span>
                    <span className="font-bold text-[#0f172a] tabular">
                      {typeof t.belt_speed_mms === "number" ? `${fmtNumber(t.belt_speed_mms, 0)} mm/s` : "--"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Motor Load:</span>
                    <span className="font-bold text-[#0f172a] tabular">
                      {typeof t.motor_load_pct === "number" ? `${fmtNumber(t.motor_load_pct, 1)}%` : "--"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Motor Temp:</span>
                    <span className="font-bold text-[#0f172a] tabular">
                      {typeof t.motor_temp_c === "number" ? `${fmtNumber(t.motor_temp_c, 1)} °C` : "--"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Rotary Encoder:</span>
                    <span className="font-bold text-[#0f172a] tabular">
                      {typeof t.encoder_ppr === "number" ? `${fmtNumber(t.encoder_ppr, 0)} PPR` : "--"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Photoelectric Sensor:</span>
                    <span className="font-bold text-[#0f172a]">
                      {typeof t.pe_clear === "boolean" ? (t.pe_clear ? "Clear" : "Blocked") : "--"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Current Sensor:</span>
                    <span className="font-bold text-[#0f172a] tabular">
                      {typeof t.motor_current_a === "number" ? `${fmtNumber(t.motor_current_a, 2)} A` : "--"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Temperature Sensor:</span>
                    <span className="font-bold text-[#0f172a] tabular">
                      {typeof t.ambient_temp_c === "number" ? `Ambient ${fmtNumber(t.ambient_temp_c, 0)} °C` : "--"}
                    </span>
                  </div>
                </div>
              </div>

              {/* Visual Evidence Snapshot */}
              <div className="space-y-2">
                <div className="font-mono text-[11px] font-bold uppercase tracking-wider text-slate-600">
                  VISUAL EVIDENCE SNAPSHOT
                </div>
                <div className="aspect-[16/10] w-full overflow-hidden rounded-[2px] border border-[#94a3b8] bg-[#0b1320] flex items-center justify-center">
                  {selected && hasSnapshot ? (
                    <img
                      src={api.snapshotUrl(selected.id)}
                      alt={`Snapshot for ${selected.id}`}
                      className="h-full w-full object-contain"
                    />
                  ) : (
                    <span className="font-mono text-[11px] text-slate-400">No snapshot attached</span>
                  )}
                </div>
              </div>

              {/* Telemetry Payload */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-slate-600">
                    TELEMETRY PAYLOAD
                  </span>
                  <button
                    type="button"
                    disabled={!selected}
                    onClick={() => void handleCopyJson()}
                    className="inline-flex items-center gap-1.5 rounded-[2px] border border-[#b8c4d4] bg-white px-2.5 py-1 font-mono text-[11px] font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                  >
                    {copied ? <Check className="h-3 w-3 text-[#15803d]" /> : <Copy className="h-3 w-3" />}
                    <span>{copied ? "Copied" : "Copy JSON"}</span>
                  </button>
                </div>
                <pre className="max-h-60 overflow-auto rounded-[2px] bg-[#0d1829] p-3.5 font-mono text-[11px] leading-relaxed text-[#7dd3fc]">
                  {selected ? JSON.stringify(selected.payload ?? selected, null, 2) : "{}"}
                </pre>
              </div>
            </div>
          </aside>
        )}
      </main>
    </div>
  );
}
