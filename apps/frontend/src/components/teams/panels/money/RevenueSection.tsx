"use client";

// Money's subject: the revenue trend, directly under the masthead.
//
// The owner's verdict on the old page — "graphs are always better than lists,
// and a list of all of the payments doesn't help" — is why this sits above the
// ledger and why the ledger is now a five-row preview. The trend is the one
// thing this page can say that the Stripe Dashboard cannot say better.
//
// ── ONE REQUEST, THREE PERIODS ────────────────────────────────────────────
// The page already reads the 90-day rollup (MoneyPanel's masthead figure), and
// `rollupWindow` deliberately asks for DOUBLE the span — 180 rows. Every one of
// 7 / 30 / 90 and its equal-length previous period is inside that, so the period
// toggle slices rows already in hand: no second SWR key, no refetch, and no
// loading flash between periods.
//
// Every figure here comes from `days[].revenue_microcents` and nothing else.
// The payments/customers/products routes are cursor pages with no totals, so
// summing them into a "total revenue" would be a number the API never gave us.

import { useState, type ReactNode } from "react";
import { PanelTabs } from "@/components/teams/shell";
import { RevenueBars, formatShortDay } from "@/components/teams/shared/RevenueBars";
import { useCompanyRollup, dayOffset, type RollupDay, type RollupSpan } from "@/hooks/useCompanyRollup";
import { MoneySection, MoneyEmptyLine } from "./MoneySection";
import { formatIsoDay, formatMicrocents } from "./money";

const SPANS = [
  { value: "7", label: "7d" },
  { value: "30", label: "30d" },
  { value: "90", label: "90d" },
];

/** The span the page fetches. The toggle only slices what this returns. */
const FETCH_SPAN: RollupSpan = 90;

/** The offered periods, narrowest first — `widenToCover` walks these in order. */
const OFFERED_SPANS: readonly RollupSpan[] = [7, 30, 90];

/** The smallest offered period that still contains `day`, or the chosen one if
 *  it already does (and the widest if nothing does). */
export function widenToCover(
  day: string,
  chosen: RollupSpan,
  end: string | null,
): RollupSpan {
  if (!end) return chosen;
  const covers = (s: RollupSpan) => day >= dayOffset(end, -(s - 1));
  if (covers(chosen)) return chosen;
  return OFFERED_SPANS.find((s) => s >= chosen && covers(s)) ?? FETCH_SPAN;
}

function sum(days: RollupDay[]): number {
  return days.reduce((acc, d) => acc + (d.revenue_microcents ?? 0), 0);
}

/** The change against the previous period of equal length — in dollars, not a
 *  percentage: revenue can be zero or negative, and a percentage off either
 *  baseline is either undefined or actively misleading. */
export function comparison(
  total: number,
  previous: number | null,
  span: number,
): string {
  if (previous === null) return `No previous ${span} days to compare against`;
  const delta = total - previous;
  if (delta === 0) return `Level with ${formatMicrocents(previous)} in the previous ${span} days`;
  return `${delta > 0 ? "Up" : "Down"} ${formatMicrocents(Math.abs(delta))} from ${formatMicrocents(previous)} in the previous ${span} days`;
}

export function RevenueSection({ day, onDayCleared }: { day: string | null; onDayCleared: () => void }) {
  const [chosenSpan, setSpan] = useState<RollupSpan>(30);
  const { rollup, isLoading, error, isUnavailable } = useCompanyRollup(FETCH_SPAN);

  // A ?day deep link can name a day older than the chosen period — a bookmark,
  // or a link an agent built. Widening to the smallest period that CONTAINS
  // that day keeps the chart honest about what the ledger below is showing;
  // leaving it at 30 would scope the payments to a day with no bar to point
  // at. Beyond 90 the day is outside the fetch entirely, so the widest period
  // is the closest we can get and the ledger still speaks for itself.
  const span = day ? widenToCover(day, chosenSpan, rollup?.end ?? null) : chosenSpan;

  // The server's own range end, not the browser clock: a viewer in UTC-7 is
  // still on the 31st when the server has rolled to the 1st, and slicing
  // against the wrong day silently drops a row off the end of the window.
  const periodStart = rollup ? dayOffset(rollup.end, -(span - 1)) : null;
  const prevStart = periodStart ? dayOffset(periodStart, -span) : null;
  const days = rollup && periodStart ? rollup.days.filter((d) => d.day >= periodStart) : [];
  // `first_day` is the first row IN THE REQUESTED RANGE (useCompanyRollup
  // contract 3), so this tests that the rollup reaches back past the start of
  // the previous period. Uncovered ⇒ no comparison at all, never a 0 for days
  // nobody measured — there is no backfill.
  const previousCovered = !!rollup?.first_day && !!prevStart && rollup.first_day <= prevStart;
  const previous =
    previousCovered && periodStart && prevStart
      ? sum(rollup!.days.filter((d) => d.day >= prevStart && d.day < periodStart))
      : null;

  const rangeLabel =
    rollup?.first_day && periodStart && rollup.first_day > periodStart
      ? `Since ${formatShortDay(rollup.first_day)}`
      : `Last ${span} days`;

  let body: ReactNode;
  // SWR keeps `data` and sets `error` when a REVALIDATION fails, so a
  // populated chart survives one transient 500 rather than being replaced by
  // a failure line.
  if (error && !rollup) {
    body = (
      <MoneyEmptyLine role="alert">
        We couldn&apos;t read your revenue just now. This is a temporary read failure, not a zero — check back shortly.
      </MoneyEmptyLine>
    );
  } else if (!rollup || isLoading) {
    body = <div className="h-32" aria-hidden="true" />;
  } else if (!rollup.stripe_bound) {
    body = <MoneyEmptyLine>Connect Stripe to see revenue here — this chart fills in on its own once payments are set up.</MoneyEmptyLine>;
  } else if (isUnavailable("revenue_microcents")) {
    // Named in `unavailable[]` means the backend knows it cannot serve the
    // column. It is omitted from days[], so summing it would render a 0
    // nobody measured.
    body = <MoneyEmptyLine>Revenue isn&apos;t available for this account.</MoneyEmptyLine>;
  } else if (rollup.first_day === null) {
    // No stored rows at all. An empty chart would read as a flat zero line —
    // a claim about the business, when the truth is that nothing has been read.
    body = <MoneyEmptyLine>No daily readings yet. The first one lands within the hour.</MoneyEmptyLine>;
  } else if (days.length === 0) {
    body = <MoneyEmptyLine>No daily readings in the last {span} days.</MoneyEmptyLine>;
  } else {
    body = (
      <div className="flex flex-col gap-1 px-2 py-2">
        <p className="font-editorial-serif text-4xl leading-none tabular-nums text-[var(--midnight)]">
          {formatMicrocents(sum(days))}
        </p>
        <p className="text-caption text-[var(--midnight-muted)]">{comparison(sum(days), previous, span)}</p>
        <RevenueBars days={days} selectedDay={day} className="mt-2 h-24 w-full" />
        {!day && (
          <p className="text-caption text-[var(--midnight-muted)]">
            Pick a day to see the payments that landed on it.
          </p>
        )}
      </div>
    );
  }

  const right = (
    <>
      {day ? (
        <>
          <span>Showing {formatIsoDay(day)}</span>
          <button
            type="button"
            onClick={onDayCleared}
            className="underline-offset-4 hover:text-[var(--midnight)] hover:underline"
          >
            Show the whole period
          </button>
        </>
      ) : (
        <span>{rangeLabel}</span>
      )}
      <PanelTabs
        variant="pill"
        ariaLabel="Period"
        tabs={SPANS}
        value={String(span)}
        onValueChange={(v) => setSpan(Number(v) as RollupSpan)}
      />
    </>
  );

  return (
    <MoneySection id="revenue" label="Revenue" right={right}>
      {body}
    </MoneySection>
  );
}
