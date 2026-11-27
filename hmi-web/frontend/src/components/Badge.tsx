import type { ReactNode } from "react";
import { clsx } from "clsx";
import { TONE_CLASSES, type Tone } from "../lib/format";

interface BadgeProps {
  tone?: Tone;
  dot?: boolean;
  pulse?: boolean;
  children: ReactNode;
  className?: string;
}

export function Badge({ tone = "muted", dot = false, pulse = false, children, className }: BadgeProps) {
  const c = TONE_CLASSES[tone];
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide tabular",
        c.bg,
        c.border,
        c.text,
        className,
      )}
    >
      {dot && <span className={clsx("h-1.5 w-1.5 rounded-full shrink-0", c.dot, pulse && "animate-pulse-dot")} />}
      {children}
    </span>
  );
}
