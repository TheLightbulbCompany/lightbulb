// apps/frontend/src/components/teams/dashboard/MetricCard.tsx

// Ported from upstream Paperclip's MetricCard
// (paperclip/ui/src/components/MetricCard.tsx) (MIT, (c) 2025 Paperclip AI).

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
  /** Receives the click event so callers can keep in-shell navigation
   * client-side (preventDefault + pushState) while `to` stays the native
   * new-tab / middle-click fallback. */
  onClick?: (e: React.MouseEvent<HTMLElement>) => void;
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
      className={`group h-full px-5 py-5 sm:px-6 sm:py-6 transition-colors${
        isClickable ? " cursor-pointer" : ""
      }`}
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <p
          className="label-caps"
          style={{ color: "var(--midnight-muted)" }}
        >
          {label}
        </p>
        <Icon size={16}
          className="shrink-0 mt-0.5 transition-colors"
          style={{ color: "var(--midnight-muted)" }}
          strokeWidth={1.75}
        />
      </div>
      <p
        className="font-editorial-serif text-4xl sm:text-5xl tabular-nums leading-none"
        style={{ color: "var(--midnight)" }}
      >
        {value}
      </p>
      {description && (
        <div
          className="text-xs mt-2.5 tabular-nums leading-snug"
          style={{ color: "var(--midnight-soft)" }}
        >
          {description}
        </div>
      )}
    </div>
  );

  if (to) {
    return (
      <Link
        href={to}
        prefetch={false}
        className="no-underline text-inherit h-full block"
        onClick={onClick}
      >
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
