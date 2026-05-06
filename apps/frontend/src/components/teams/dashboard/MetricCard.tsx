// apps/frontend/src/components/teams/dashboard/MetricCard.tsx

// Ported from upstream Paperclip's MetricCard
// (paperclip/ui/src/components/MetricCard.tsx) (MIT, (c) 2025 Paperclip AI).
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

export interface MetricCardProps {
  icon: LucideIcon;
  value: string | number;
  label: string;
  description?: ReactNode;
  /** Optional: when set, the card becomes a link (`next/link`). */
  to?: string;
  onClick?: () => void;
}

/**
 * Compact dashboard tile rendering a single metric (value + label, optional
 * description) with a leading `lucide-react` icon. Becomes a clickable
 * `next/link` when `to` is provided. Translated 1:1 from upstream's
 * `MetricCard.tsx`; the only deviation is `Link from @/lib/router` ->
 * `next/link` for the App Router.
 */
export function MetricCard({ icon: Icon, value, label, description, to, onClick }: MetricCardProps) {
  const isClickable = !!(to || onClick);

  const inner = (
    <div
      className={`h-full px-4 py-4 sm:px-5 sm:py-5 rounded-lg transition-colors${
        isClickable ? " hover:bg-accent/50 cursor-pointer" : ""
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <p className="text-2xl sm:text-3xl font-semibold tracking-tight tabular-nums">
            {value}
          </p>
          <p className="text-xs sm:text-sm font-medium text-muted-foreground mt-1">
            {label}
          </p>
          {description && (
            <div className="text-xs text-muted-foreground/70 mt-1.5 hidden sm:block">
              {description}
            </div>
          )}
        </div>
        <Icon className="h-4 w-4 text-muted-foreground/50 shrink-0 mt-1.5" />
      </div>
    </div>
  );

  if (to) {
    return (
      <Link href={to} className="no-underline text-inherit h-full" onClick={onClick}>
        {inner}
      </Link>
    );
  }

  if (onClick) {
    return (
      <div className="h-full" onClick={onClick}>
        {inner}
      </div>
    );
  }

  return inner;
}
