import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface PanelMastheadProps {
  overline: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
  /** Heading level — h1 by default; the landing-page demo passes h2 so the hero keeps the page's only h1. */
  as?: "h1" | "h2";
}

export function PanelMasthead({
  overline,
  title,
  meta,
  actions,
  className,
  as: Heading = "h1",
}: PanelMastheadProps) {
  return (
    <header
      className={cn(
        "flex flex-col gap-2 pb-2 sm:flex-row sm:items-end sm:justify-between",
        // In-flow, not sticky: this scrolls WITH the panel body inside
        // .workspace-panel-page, so nothing ever passes under it. A hard
        // --edge rule is the right separator here; a scroll-edge fade would
        // imply an overlap that does not exist.
        "border-b border-[var(--edge)]",
        className,
      )}
    >
      <div className="flex items-baseline gap-2">
        <Heading className="font-editorial-serif text-2xl leading-none text-[var(--midnight)]">
          {title}
        </Heading>
        {overline !== title && (
          <p className="label-caps">
            {overline}
          </p>
        )}
      </div>
      {(meta || actions) && (
        <div className="flex items-center gap-3 sm:pb-1">
          {meta && (
            <p
              data-testid="panel-masthead-meta"
              className="text-caption tabular-nums font-mono text-[var(--midnight-muted)]"
            >
              {meta}
            </p>
          )}
          {actions}
        </div>
      )}
    </header>
  );
}
