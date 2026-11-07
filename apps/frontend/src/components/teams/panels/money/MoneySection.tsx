"use client";

// The chrome every Money section shares: a mono-caps label with ONE right-hand
// line (a count, a scope, or a link), then the body. Sections, not sub-tabs
// (spec §2): the shell already spends the tab metaphor on open panels.

import type { ReactNode } from "react";

export function MoneySection({ id, label, right, children }: { id: string; label: string; right?: ReactNode; children: ReactNode }) {
  // A sentence-case head in Satoshi, no caps (dev walk W12-14).
  return (
    <section id={`money-${id}`} aria-label={label} className="flex min-w-0 flex-col gap-2 scroll-mt-4">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className="text-ui font-medium text-[var(--midnight)]">{label}</p>
        {right && <div className="flex items-center gap-3 text-caption text-[var(--midnight-muted)]">{right}</div>}
      </div>
      {children}
    </section>
  );
}

/** The one calm line for an empty list — or, with `role="alert"`, for a read
 *  that failed. One vocabulary either way: a single italic serif sentence. */
export function MoneyEmptyLine({ children, role = "status" }: { children: ReactNode; role?: "status" | "alert" }) {
  return (
    <p role={role} className="px-2 py-3 text-ui text-[var(--midnight-muted)]">
      {children}
    </p>
  );
}
