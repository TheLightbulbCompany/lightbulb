import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface PanelMastheadProps {
  overline: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

export function PanelMasthead({
  overline,
  title,
  meta,
  actions,
  className,
}: PanelMastheadProps) {
  return (
    <header
      className={cn(
        "flex flex-col gap-3 pb-3 sm:flex-row sm:items-end sm:justify-between",
        "border-b border-[var(--edge)]",
        className,
      )}
    >
      <div className="space-y-1">
        <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--mute)]">
          {overline}
        </p>
        <h1 className="font-editorial-serif text-5xl leading-none text-[var(--ink)]">
          {title}
        </h1>
      </div>
      <div className="flex items-center gap-3 sm:pb-1">
        {meta && (
          <p className="text-xs tabular-nums font-mono text-[var(--mute)]">{meta}</p>
        )}
        {actions}
      </div>
    </header>
  );
}
