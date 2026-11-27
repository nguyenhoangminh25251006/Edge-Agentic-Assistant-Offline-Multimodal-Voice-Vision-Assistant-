import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { useHmi } from "../store/HmiContext";

interface StationBarProps {
  left?: ReactNode;
  right?: ReactNode;
}

export function StationBar({ left, right }: StationBarProps) {
  const { station, system } = useHmi();
  const active = station ?? system?.station;
  const stations = system?.stations ?? (active ? [active] : []);

  return (
    <div className="border-b border-[#cbd5e1] bg-[#f4f7fa]">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-2 px-5 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="relative inline-flex items-center rounded-[2px] border border-[#b8c4d4] bg-white px-2 py-1 font-mono text-[11px]">
            <span className="text-slate-500 mr-1.5">Station:</span>
            {stations.length > 1 ? (
              <select
                aria-label="Active station"
                className="appearance-none bg-transparent pr-5 text-ink focus:outline-none"
                defaultValue={active?.id}
              >
                {stations.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.id} ({s.host})
                  </option>
                ))}
              </select>
            ) : (
              <span>
                <strong className="font-bold text-slate-900">{active?.id ?? "--"}</strong>{" "}
                <span className="text-slate-500">({active?.host ?? "--"})</span>
              </span>
            )}
            <ChevronDown className="ml-2 h-3.5 w-3.5 text-slate-500 pointer-events-none" />
          </div>
          {left}
        </div>
        {right && <div className="flex items-center gap-2">{right}</div>}
      </div>
    </div>
  );
}
