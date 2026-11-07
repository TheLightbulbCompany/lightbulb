"use client";

// The one revenue chart. Home's business hero and Money's subject render THIS
// — a second implementation would drift on the four things that took a review
// cycle each to get right: the clamped bar heights, the zero baseline for
// refund days, the per-bar <title>, and `role="group"` (not `img`, which makes
// the subtree presentational and hides every anchor from assistive tech).
//
// The bars are the chart-as-filter: each is an SVG <a> to Money scoped to that
// day, navigated in-shell by handleShellLinkClick, with the anchor's
// aria-label carrying the same day-and-amount text as the hover <title>.

import type { RollupDay } from "@/hooks/useCompanyRollup";
import { formatMicrocents } from "@/components/teams/panels/money/money";
import { handleShellLinkClick } from "@/lib/shellNav";

/** Locale AND timezone pinned: `undefined` resolves the HOST default, so the
 *  same date renders "Aug 30" on one machine and "30 Aug" on another — and a
 *  test asserting either fails on the other runner with a failure that reads
 *  as a date-logic bug. */
export function formatShortDay(day: string): string {
  return new Date(day + "T00:00:00Z").toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export function RevenueBars({
  days,
  selectedDay,
  className = "mt-3 h-12 w-full",
}: {
  days: RollupDay[];
  /** The day the page is currently scoped to; it takes the accent instead of
   *  "today". Absent (Home) keeps the last bar accented. */
  selectedDay?: string | null;
  className?: string;
}) {
  const values = days.map((d) => (Number.isFinite(d.revenue_microcents) ? d.revenue_microcents : 0));
  if (values.length === 0) return null;
  // Revenue CAN be negative: `_read_day` sums balance-transaction `net` over
  // all transaction types, so a day whose refunds exceed its charges is below
  // zero. A max-only scale put those bars outside the viewBox, where the SVG
  // clipped them silently — a refund day rendered as a flat line.
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values, 0);
  const range = hi - lo || 1;
  const zeroY = 20 - ((0 - lo) / range) * 20;
  const slot = 100 / values.length;
  const gap = slot * 0.25;
  return (
    <svg viewBox="0 0 100 20" preserveAspectRatio="none" role="group" aria-label="Revenue over the period" className={className}>
      {values.map((v, i) => {
        // Clamp ONCE, use the clamped value for both y and height — a zero
        // (or tiny) day drew from the unclamped y with a clamped height,
        // which stranded its 0.2-tall sliver below the baseline (or off the
        // viewBox entirely) instead of resting on it.
        const h = Math.max((Math.abs(v) / range) * 20, 0.2);
        // Clamped at 0: in an all-non-positive window `zeroY` sits at the very
        // top (0), and an unclamped zero-day sliver drew upward from there to
        // y=-0.2, outside the viewBox.
        const y = v >= 0 ? Math.max(zeroY - h, 0) : zeroY;
        const label = `${formatShortDay(days[i].day)} · ${formatMicrocents(v)}`;
        const href = `/chat?panel=teams-money&day=${days[i].day}`;
        const accent = selectedDay ? days[i].day === selectedDay : i === values.length - 1;
        return (
          <a
            key={days[i].day}
            href={href}
            onClick={(e) => handleShellLinkClick(e, href)}
            aria-label={label}
            aria-current={selectedDay && days[i].day === selectedDay ? "true" : undefined}
          >
            <rect
              x={(i * slot + gap / 2).toFixed(2)}
              y={y.toFixed(2)}
              width={(slot - gap).toFixed(2)}
              height={h.toFixed(2)}
              fill={accent ? "var(--midnight)" : "var(--chart-5)"}
            >
              <title>{label}</title>
            </rect>
          </a>
        );
      })}
      <line x1="0" x2="100" y1={zeroY.toFixed(2)} y2={zeroY.toFixed(2)} stroke="var(--edge)" strokeWidth="0.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
