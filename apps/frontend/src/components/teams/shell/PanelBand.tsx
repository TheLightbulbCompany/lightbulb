"use client";

// The chrome every page section shares: a mono-caps label with ONE right-hand
// line (a count, a scope, or a link), then the body. Sections, not sub-tabs
// (spec §2): the shell already spends the tab metaphor on open panels.
//
// `id` is the FULL dom id — `money-payments`, `inbox-needs-you` — because a
// ?section deep link scrolls to it by that exact string.
//
// Money still renders its own local copy (money/MoneySection.tsx). That file
// is being rewritten on a parallel branch; it migrates onto this primitive in
// a follow-up rather than colliding with that work.

import type { ReactNode } from "react";

export interface PanelBandProps {
  id: string;
  label: string;
  right?: ReactNode;
  children: ReactNode;
}

export function PanelBand({ id, label, right, children }: PanelBandProps) {
  return (
    <section id={id} aria-label={label} className="flex min-w-0 flex-col gap-2 scroll-mt-4">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className="label-caps text-[var(--midnight-muted)]">{label}</p>
        {right && (
          <div className="flex items-center gap-3 text-caption text-[var(--midnight-muted)]">{right}</div>
        )}
      </div>
      {children}
    </section>
  );
}

/** The one calm line for an empty, loading or failed section. */
export function PanelBandEmpty({
  children,
  role = "status",
}: {
  children: ReactNode;
  role?: "status" | "alert";
}) {
  return (
    <p
      role={role}
      className="px-2 py-3 font-editorial-serif text-base italic text-[var(--midnight-muted)]"
    >
      {children}
    </p>
  );
}
