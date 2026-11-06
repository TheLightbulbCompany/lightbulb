"use client";

// The chrome every Money section shares: a mono-caps label with ONE right-hand
// line (a count, a scope, or a link), then the body. Sections, not sub-tabs
// (spec §2): the shell already spends the tab metaphor on open panels.

import type { ReactNode } from "react";
import { useSuperAgent } from "@/hooks/useLiveEvents";

export function MoneySection({ id, label, right, children }: { id: string; label: string; right?: ReactNode; children: ReactNode }) {
  // Super-agent: a sentence-case head in Satoshi, no caps (dev walk W12-14).
  const superAgent = useSuperAgent();
  return (
    <section id={`money-${id}`} aria-label={label} className="flex min-w-0 flex-col gap-2 scroll-mt-4">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className={superAgent ? "text-ui font-medium text-[var(--midnight)]" : "label-caps text-[var(--midnight-muted)]"}>{label}</p>
        {right && <div className="flex items-center gap-3 text-caption text-[var(--midnight-muted)]">{right}</div>}
      </div>
      {children}
    </section>
  );
}

/** The one calm line for an empty list — or, with `role="alert"`, for a read
 *  that failed. One vocabulary either way: a single italic serif sentence. */
export function MoneyEmptyLine({ children, role = "status" }: { children: ReactNode; role?: "status" | "alert" }) {
  const superAgent = useSuperAgent();
  return (
    <p role={role} className={superAgent ? "px-2 py-3 text-ui text-[var(--midnight-muted)]" : "px-2 py-3 font-editorial-serif text-base italic text-[var(--midnight-muted)]"}>
      {children}
    </p>
  );
}
