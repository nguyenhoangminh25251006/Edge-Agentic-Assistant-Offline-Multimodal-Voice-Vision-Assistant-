import type { ReactNode } from "react";
import { clsx } from "clsx";

interface CardProps {
  title?: string;
  subtitle?: string;
  icon?: ReactNode;
  headerRight?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

export function Card({ title, subtitle, icon, headerRight, children, className, bodyClassName }: CardProps) {
  return (
    <section className={clsx("rounded border border-line bg-white shadow-card flex flex-col", className)}>
      {(title || headerRight) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <div className="flex items-center gap-2 min-w-0">
            {icon && <span className="text-ink-muted shrink-0">{icon}</span>}
            <div className="min-w-0">
              {title && <h2 className="text-sm font-semibold text-ink truncate">{title}</h2>}
              {subtitle && <p className="text-[11px] text-ink-faint truncate">{subtitle}</p>}
            </div>
          </div>
          {headerRight && <div className="flex items-center gap-2 shrink-0">{headerRight}</div>}
        </header>
      )}
      <div className={clsx("p-4", bodyClassName)}>{children}</div>
    </section>
  );
}
