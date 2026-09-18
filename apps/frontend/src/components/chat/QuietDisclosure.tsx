"use client";

import * as React from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * One quiet line that hides a block until asked.
 *
 * Exists because a native `<details>/<summary>` draws the browser's own
 * disclosure marker: a large, unstyled, un-themeable triangle that ignored the
 * design system and — next to a folded `ToolActivityTrace` — was the loudest
 * thing in the transcript. This is the same treatment the trace's own fold uses
 * (3px chevron, muted label, rotate on open), so a run's secondary blocks all
 * read as one family instead of two.
 *
 * Nothing is destroyed, only folded: the children mount on open, and the
 * trigger is a real button carrying `aria-expanded`, so screen readers and
 * keyboards get the same affordance `<summary>` gave them.
 */
export function QuietDisclosure({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-left text-ui text-[var(--midnight-muted)] transition-colors hover:text-[var(--midnight-soft)]"
      >
        <ChevronRight
          aria-hidden
          className={cn(
            "h-3 w-3 shrink-0 text-[var(--midnight-faint)] transition-transform",
            open && "rotate-90",
          )}
        />
        {label}
      </button>
      {open && (
        <div className="mt-1 border-l border-[var(--edge)] pl-3">{children}</div>
      )}
    </div>
  );
}
