"use client";

// The business strip above Home's agent activity.
//
// Home answers "is my company moving". Today's DashboardPanel is a port of
// Paperclip's dashboard and answers "are my agents busy" — runs, priority,
// issue status, success rate. Both are worth knowing; only one of them is the
// question an owner opens the product with.
//
// ADDITIVE ON PURPOSE. This renders ABOVE the existing dashboard rather than
// replacing it, so an owner with no Stripe binding sees today's Home unchanged
// plus one honest notice, and nobody loses a screen they were using.
//
// ── WHY THE SPARKLINES ARE MONOCHROME ─────────────────────────────────────
// Result colour-codes its Home tiles (green revenue, blue visitors, pink
// followers…). We deliberately do not. `.interface-design/system.md` reserves
// the state palette for status — ok / info / warn / err — and says outright
// that the fifth chart token is `--muted-foreground`, "not a state colour, so
// no non-status series borrows one". Revenue is not "ok"; visitors are not
// "info". Colouring them would spend the one signal we have for real incidents
// on decoration, and then a genuinely degraded tile would have nothing left to
// say it with.

import { DollarSign, UserPlus, Eye, Wallet } from "lucide-react";
import { ApiError } from "@lightbulb/shared";
import { PanelNotice } from "@/components/teams/shell";
import { useCompanyRollup, type RollupDay } from "@/hooks/useCompanyRollup";
import { formatUsdCents } from "@/lib/creditAmounts";
import { handleShellLinkClick, teamsPanelUrl } from "@/lib/shellNav";

/** Microcents → cents. The rollup speaks microcents; `formatUsdCents` wants cents. */
function microcentsToCents(v: number): number {
  return Math.round(v / 10_000);
}

function sum(days: RollupDay[], key: "revenue_microcents" | "signups" | "visitors"): number {
  return days.reduce((acc, d) => acc + (d[key] ?? 0), 0);
}

/**
 * A bare polyline. No chart library: this is five to thirty points with no
 * axes, no tooltip and no legend, and pulling in a charting dependency to draw
 * one line would be the kind of thing nobody removes later.
 */
function Sparkline({ values: raw, label }: { values: number[]; label: string }) {
  // `sum()` guards with `?? 0`; without the same coercion here one missing key
  // makes min/max NaN and every coordinate serialises as "NaN,NaN" — the SVG
  // then renders nothing and the tile silently shows a total with no trend.
  const values = raw.map((v) => (Number.isFinite(v) ? v : 0));
  if (values.length < 2) return null;
  // Revenue CAN be negative: `_read_day` sums balance-transaction `net` over
  // all transaction types, so a day whose refunds and disputes exceed its
  // charges is below zero. A `max`-only scale put those points outside the
  // viewBox, where the SVG clipped them silently — a refund day rendered as a
  // flat line instead of a drop, which is worse than not drawing it at all.
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values, 0);
  const span = hi - lo || 1;
  const step = 100 / (values.length - 1);
  const points = values
    .map((v, i) => `${(i * step).toFixed(2)},${(19 - ((v - lo) / span) * 18).toFixed(2)}`)
    .join(" ");
  return (
    <svg
      viewBox="0 0 100 20"
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
      className="mt-2 h-5 w-full"
    >
      <polyline
        points={points}
        fill="none"
        stroke="var(--chart-5)"
        strokeWidth="1"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function Tile({
  icon: Icon,
  label,
  value,
  caption,
  series,
  unavailable,
}: {
  icon: typeof DollarSign;
  label: string;
  value: string;
  caption: string;
  series?: number[];
  unavailable?: boolean;
}) {
  return (
    <div className="rounded-md border border-[var(--edge)] bg-card px-4 py-3">
      <div className="mb-1.5 flex items-start justify-between gap-2">
        <p className="label-caps text-[var(--midnight-muted)]">{label}</p>
        <Icon size={14} strokeWidth={1.75} className="mt-0.5 shrink-0 text-[var(--midnight-muted)]" />
      </div>
      {/* An unavailable column must not print the computed value. The backend
          omits the key from days[], so `sum()` coerces it to 0 — and a 0 here
          is a claim we have not earned, which is the whole point of the
          `unavailable` list. Say we cannot see it. */}
      <p
        className={
          unavailable
            ? "font-editorial-serif text-lg italic leading-none text-[var(--midnight-muted)]"
            : "font-editorial-serif text-2xl tabular-nums leading-none text-[var(--midnight)]"
        }
      >
        {unavailable ? "Unavailable" : value}
      </p>
      <p className="mt-1.5 text-xs text-[var(--midnight-muted)]">
        {unavailable ? "Not wired up yet" : caption}
      </p>
      {series && !unavailable && <Sparkline values={series} label={`${label} over the period`} />}
    </div>
  );
}

export function HomeRollupStrip() {
  const { rollup, isLoading, error, isUnavailable } = useCompanyRollup();

  // `/company/rollup` calls require_org_admin, so an ordinary org member 403s
  // forever. Telling them it is temporary and to check back sends them to wait
  // for something that can never resolve — and SWR keeps retrying a request
  // that cannot succeed.
  if (error instanceof ApiError && error.status === 403) return null;

  if (isLoading && !rollup) return <div className="h-[132px]" aria-hidden="true" />;

  // `error && !rollup`, NOT `error || !rollup`: SWR keeps `data` and sets
  // `error` when a REVALIDATION fails, so the wider guard threw away numbers
  // still sitting in the cache and replaced a populated strip with a failure
  // notice on one transient 500.
  if (error && !rollup) {
    return (
      <PanelNotice
        state="unavailable"
        icon={DollarSign}
        headline="We couldn't read your business numbers just now."
        supporting="This is a temporary read failure, not a zero. Check back shortly."
      />
    );
  }
  if (!rollup) return null;

  // No Connect binding ⇒ no rollup rows exist at all, including the columns
  // that have nothing to do with Stripe. A silent set of zeros here would be a
  // lie: we do not know it is zero, we know we cannot see.
  if (!rollup.stripe_bound) {
    return (
      <PanelNotice
        state="unbound"
        icon={DollarSign}
        headline="Connect Stripe to see revenue here."
        supporting="Once your company's Stripe account is connected, revenue, signups and visitors fill in on their own."
        prompt="Set up payments for my business."
        href={teamsPanelUrl("accounts")}
        action={{
          label: "Set up payments",
          // Keeps the jump in-shell (preventDefault + pushState) while the
          // href stays a real URL for new-tab / middle-click.
          onClick: (e) => handleShellLinkClick(e, teamsPanelUrl("accounts")),
        }}
      />
    );
  }

  const days = rollup.days ?? [];
  const spendCents = microcentsToCents(rollup.agent_spend_month_to_date_microcents ?? 0);

  // Bound minutes ago: `stripe_bound: true` with no rows, because the hourly
  // writer has not run and there is no backfill. Rendering the tiles here put
  // "$0" beside a range label reading "No readings yet" — two contradictory
  // statements on one strip, and three numbers we had not earned.
  if (!days.length) {
    return (
      <PanelNotice
        state="zero"
        icon={DollarSign}
        headline="No readings yet."
        supporting="Your Stripe account is connected. The first daily reading lands within the hour."
      />
    );
  }

  // `days.length > 0` is NOT the same as "there is activity to show". The
  // live dev payload was three rows present, every value a real 0, visitors
  // omitted (unavailable) — and the tiles rendered "$0 / 0 / Unavailable ·
  // 3 days with readings", which reads as "we have three days of data" when
  // the truth is "three days, nothing happened". Same trap Spend fell into at
  // the month boundary. So: if every SERVED metric is zero (unavailable ones
  // do not count either way), say that in a sentence instead of a tile row.
  //
  // The asymmetry matters: an owner who earned $0 today but had 40 visitors
  // must still see the tiles — real zero revenue beside a real visitor count.
  // Only all-zero-or-unavailable collapses.
  const served = (["revenue_microcents", "signups", "visitors"] as const).filter(
    (k) => !isUnavailable(k),
  );
  const nothingHappened = served.every((k) => sum(days, k) === 0);
  if (nothingHappened) {
    return (
      <PanelNotice
        state="zero"
        icon={DollarSign}
        headline={`Nothing to report yet across ${days.length} ${days.length === 1 ? "day" : "days"} of readings.`}
        supporting="Stripe is connected and reporting; there just hasn't been a sale or a signup in the window. The strip fills in from the first one."
      />
    );
  }

  // `days[]` is SPARSE — the writer returns None and stores nothing when every
  // source was empty, so absent days are omitted rather than zero-filled. The
  // sparkline plots ordinally, which means a gap of weeks looks identical to
  // consecutive days. Rather than pretend otherwise, the caption says how many
  // days actually carry a reading; plotting against the date is the real fix
  // and is deferred until the strip is worth a charting dependency.
  const readingsCaption = days.length
    ? `${days.length} ${days.length === 1 ? "day" : "days"} with readings`
    : "No readings yet";

  // Label the range from first_day, never from the window we asked for —
  // there is no backfill, so rollups start at the Stripe binding.
  const rangeLabel = rollup.first_day
    // Locale pinned: `undefined` resolves the HOST default, so the same date
    // renders "Aug 30" on one machine and "30 Aug" on another — and a test
    // asserting either fails on the other runner with a failure that reads as
    // a date-logic bug.
    ? `Since ${new Date(rollup.first_day + "T00:00:00Z").toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        timeZone: "UTC",
      })}`
    : "No readings yet";

  return (
    <section aria-label="Business summary" className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-3">
        <p className="label-caps text-[var(--midnight-muted)]">Business</p>
        <p className="text-xs text-[var(--midnight-muted)]">{rangeLabel}</p>
      </div>

      {/* Container queries, not viewport breakpoints: this strip renders inside
          a dock pane that is often half the viewport (a browser panel sits
          beside it). With `lg:grid-cols-4`, a narrow pane still got four
          columns and "Unavailable" truncated to "Unavailab". The container
          is InlineTeamsPanel's wrapper, the same one DashboardPanel's own
          `@md:`/`@4xl:` tiles resolve against. */}
      <div className="grid grid-cols-2 gap-2 @3xl:grid-cols-4">
        <Tile
          icon={DollarSign}
          label="Revenue"
          value={formatUsdCents(microcentsToCents(sum(days, "revenue_microcents")))}
          caption={readingsCaption}
          series={days.map((d) => d.revenue_microcents)}
          unavailable={isUnavailable("revenue_microcents")}
        />
        <Tile
          icon={UserPlus}
          label="Signups"
          value={sum(days, "signups").toLocaleString()}
          caption={readingsCaption}
          series={days.map((d) => d.signups)}
          unavailable={isUnavailable("signups")}
        />
        {/* The backend's `unavailable` is owner-derived: an owner
            with no PostHog connection has `visitors` omitted from days[]
            AND named in `unavailable`, while an owner WITH PostHog and a
            genuine zero-traffic day still sends a real `visitors: 0` that is
            not in the list. Suppressing that measured zero would be the
            opposite error to the one this guard exists for.

            KNOWN LIMIT, stated in the caption rather than left to be
            discovered: the backend signal is connection-level, not per-day. An
            owner who connected PostHog and whose refresh token has since
            lapsed reads a measured-looking 0. Naming the source is what makes
            that recoverable — an owner who doubts the number knows which
            system to go and check. */}
        <Tile
          icon={Eye}
          label="Visitors"
          value={sum(days, "visitors").toLocaleString()}
          caption={isUnavailable("visitors") ? readingsCaption : "From your PostHog"}
          series={days.map((d) => d.visitors)}
          unavailable={isUnavailable("visitors")}
        />
        {/* Month-to-date, NOT a per-day series — see useCompanyRollup's note.
            There is deliberately no sparkline here: the underlying column is
            the running month total stamped on every row, so plotting it would
            draw a staircase that looks like growth. */}
        <Tile
          icon={Wallet}
          label="Agent spend"
          value={formatUsdCents(spendCents)}
          caption="Month to date"
        />
      </div>

      {/* Deliberately NOT listing rollup.unavailable here. It is the fixed
          constant ["followers","open_threads"], so the line rendered for every
          owner forever — explaining the absence of two things this strip never
          offered a tile for. `isUnavailable` still guards each tile it does
          render, which is where the contract actually bites. */}
    </section>
  );
}
