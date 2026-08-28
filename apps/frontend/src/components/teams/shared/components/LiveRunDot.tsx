"use client";

import { cn } from "@/lib/utils";
import type { LiveRunActivity } from "@/components/teams/shared/lib/liveRuns";

interface LiveRunDotProps {
  /** Healthy "running" (forest ping) vs "stalled" (amber pulse). */
  state?: LiveRunActivity;
  /** Dot diameter — `sm` = h-2 w-2 (list rows), `md` = size-2.5 (cards). */
  size?: "sm" | "md";
  /** Tooltip + a11y label; falls back to a per-state default. */
  title?: string;
  /**
   * Render as purely decorative (aria-hidden, no role). Use when the run
   * state is already announced by adjacent visible text (e.g. the Dashboard
   * card's status line) so screen readers don't hear it twice. The hover
   * `title` tooltip is kept either way.
   */
  decorative?: boolean;
  className?: string;
  testId?: string;
}

const TONE: Record<LiveRunActivity, { color: string; halo: string; label: string }> = {
  // A lively soft-forest "ping" reads as working; a slow amber "pulse" reads
  // as stuck / needs-a-look — never red (the run is still live, not failed).
  running: { color: "var(--midnight)", halo: "var(--brass)", label: "Agent running" },
  stalled: { color: "var(--warn)", halo: "var(--warn)", label: "Agent may be stuck" },
};

/**
 * Live-run status dot shared across the Teams surfaces (Inbox list, Kanban,
 * Tasks list, Dashboard active-agents, issue detail). The stalled
 * classification is upstream-authoritative (see `liveRunActivity`), not a
 * local heuristic — so a hung overnight run looks visibly stuck instead of
 * silently "Live now".
 *
 * Animation semantics: running = expanding ping halo; stalled = the dot
 * itself pulses (opacity fade). The stalled animation MUST be on the core
 * dot — `animate-pulse` has no scale, so on an equal-size halo underneath
 * the opaque core it would be fully occluded and render static.
 */
export function LiveRunDot({
  state = "running",
  size = "sm",
  title,
  decorative = false,
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
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : label}
      aria-hidden={decorative ? true : undefined}
      title={label}
    >
      {state === "running" ? (
        <span
          className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60"
          style={{ backgroundColor: tone.halo }}
          aria-hidden
        />
      ) : null}
      <span
        className={cn(
          "relative inline-flex rounded-full",
          dim,
          state === "stalled" && "animate-pulse",
        )}
        style={{ backgroundColor: tone.color }}
        aria-hidden
      />
    </span>
  );
}
