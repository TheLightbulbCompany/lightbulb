"use client";

import { cn } from "@/lib/utils";
import type { LiveRunActivity } from "@/components/teams/dashboard/hooks/useLiveRuns";

interface LiveRunDotProps {
  /** Healthy "running" (forest ping) vs "stalled" (amber pulse). */
  state?: LiveRunActivity;
  /** Dot diameter — `sm` = h-2 w-2 (list rows), `md` = size-2.5 (cards). */
  size?: "sm" | "md";
  /** Tooltip + a11y label; falls back to a per-state default. */
  title?: string;
  className?: string;
  testId?: string;
}

const TONE: Record<LiveRunActivity, { color: string; anim: string; label: string }> = {
  // A lively forest "ping" reads as working; a slow amber "pulse" reads as
  // stuck / needs-a-look — never red (the run is still live, not failed).
  running: { color: "var(--forest)", anim: "animate-ping", label: "Agent running" },
  stalled: { color: "var(--warn)", anim: "animate-pulse", label: "Agent may be stuck" },
};

/**
 * Live-run status dot shared across the Teams surfaces (Inbox list, Dashboard
 * active-agents, issue detail). The stalled classification is upstream-
 * authoritative (see `liveRunActivity`), not a local heuristic — so a hung
 * overnight run looks visibly stuck instead of silently "Live now".
 */
export function LiveRunDot({
  state = "running",
  size = "sm",
  title,
  className,
  testId,
}: LiveRunDotProps) {
  const tone = TONE[state];
  const dim = size === "md" ? "size-2.5" : "h-2 w-2";
  const label = title ?? tone.label;
  return (
    <span
      data-testid={testId}
      className={cn("relative inline-flex shrink-0", dim, className)}
      role="img"
      aria-label={label}
      title={label}
    >
      <span
        className={cn("absolute inline-flex h-full w-full rounded-full opacity-70", tone.anim)}
        style={{ backgroundColor: tone.color }}
        aria-hidden
      />
      <span
        className={cn("relative inline-flex rounded-full", dim)}
        style={{ backgroundColor: tone.color }}
        aria-hidden
      />
    </span>
  );
}
