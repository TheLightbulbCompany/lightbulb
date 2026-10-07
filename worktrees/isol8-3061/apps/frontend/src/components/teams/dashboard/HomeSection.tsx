"use client";

// The chrome every Home section shares: a mono-caps label with one right-hand
// link into the detail page, rows that are one anchor each, and a single
// italic line for loading / error / nothing. Three sections use all three, so
// this is the one place their rhythm is decided (system.md: calm when
// healthy — no coloured states here; `HomeRollupStrip` owns the loud ones).

import type { MouseEvent, ReactNode } from "react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { handleShellLinkClick } from "@/lib/shellNav";

export function HomeSection({
  label,
  href,
  linkLabel,
  children,
}: {
  label: string;
  href?: string;
  linkLabel?: string;
  children: ReactNode;
}) {
  return (
    <section aria-label={label} className="flex min-w-0 flex-col">
      <div className="flex items-baseline justify-between gap-3 border-b border-[var(--edge-soft)] pb-1.5">
        <p className="label-caps text-[var(--midnight-muted)]">{label}</p>
        {href && linkLabel && (
          <a
            href={href}
            onClick={(e: MouseEvent<HTMLAnchorElement>) => handleShellLinkClick(e, href)}
            className="text-xs text-[var(--midnight-muted)] underline-offset-4 hover:text-[var(--midnight)] hover:underline"
          >
            {linkLabel}
          </a>
        )}
      </div>
      <div className="flex flex-col">{children}</div>
    </section>
  );
}

export function HomeRow({
  who,
  title,
  sub,
  right,
  href,
}: {
  /** Whose row this is — an agent name for the avatar initials. */
  who: string;
  title: ReactNode;
  sub?: ReactNode;
  right?: ReactNode;
  /** In-shell destination; stays a real href for new-tab / middle-click. */
  href: string;
}) {
  return (
    <a
      href={href}
      onClick={(e: MouseEvent<HTMLAnchorElement>) => handleShellLinkClick(e, href)}
      className="grid grid-cols-[28px_1fr_auto] items-center gap-3 border-b border-[var(--edge-soft)] px-2 py-2.5 no-underline transition-colors last:border-b-0 hover:bg-[var(--cream-deep)]"
      style={{ color: "var(--midnight)" }}
    >
      <Identity name={who} size="sm" avatarOnly />
      <span className="min-w-0">
        <span className="block truncate text-sm">{title}</span>
        {sub && (
          <span className="block truncate text-xs" style={{ color: "var(--midnight-muted)" }}>
            {sub}
          </span>
        )}
      </span>
      {right !== undefined && (
        <span className="shrink-0 text-xs tabular-nums" style={{ color: "var(--midnight-muted)" }}>
          {right}
        </span>
      )}
    </a>
  );
}

export function HomeRowsEmpty({
  children,
  role = "status",
}: {
  children: ReactNode;
  role?: "status" | "alert";
}) {
  return (
    <p
      role={role}
      className="px-2 py-3 font-editorial-serif text-base italic"
      style={{ color: "var(--midnight-muted)" }}
    >
      {children}
    </p>
  );
}
