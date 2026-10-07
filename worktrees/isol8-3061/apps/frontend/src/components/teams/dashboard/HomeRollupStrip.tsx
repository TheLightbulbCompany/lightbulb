"use client";

// Home's business hero.
//
// Home answers "is my company moving" before it answers "are my agents busy"
// — Inbox and Tasks own that second question now. This strip is the
// first one: revenue, new customers, visitors and
// agent spend, each an anchored figure (spec §2) rather than a dead number —
// every tile carries an "Ask CEO" link that hands the owner's question,
// with the period already worded into it, straight to the CEO agent's
// composer. The hero's bars are hover-labelled (a native <title> per bar) so
// a specific day reads exactly, which a bare sparkline cannot answer.
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
import { useState, type ReactNode } from "react";
import { PanelNotice, PanelTabs } from "@/components/teams/shell";
import { useCompanyStripeStatus } from "@/components/settings/CompanyStripeSection";
import { useCompanyRollup, type RollupDay, type RollupSpan } from "@/hooks/useCompanyRollup";
import { RevenueBars, formatShortDay } from "@/components/teams/shared/RevenueBars";
import { useAskCeo } from "@/hooks/useAskCeo";
import { formatUsdCents } from "@/lib/creditAmounts";
import { handleShellLinkClick, teamsPanelUrl } from "@/lib/shellNav";

/** Microcents → cents. The rollup speaks microcents; `formatUsdCents` wants cents. */
function microcentsToCents(v: number): number {
  return Math.round(v / 10_000);
}

function sum(days: RollupDay[], key: "revenue_microcents" | "signups" | "visitors"): number {
  return days.reduce((acc, d) => acc + (d[key] ?? 0), 0);
}

/** `formatUsdCents(-500)` renders "$-5". Revenue is net and can go negative
 *  (refund days), so the sign goes in front of the currency, typographic minus.
 *  Escaped, not literal: U+2212 is indistinguishable from a hyphen in source. */
function formatSignedUsdCents(cents: number): string {
  return cents < 0 ? `\u2212${formatUsdCents(-cents)}` : formatUsdCents(cents);
}

/** Locale pinned — `toLocaleString()` alone renders 1,234 or 1.234 by host. */
function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}

/**
 * A bare polyline. No chart library: this is five to thirty points with no
 * axes, no tooltip and no legend, and pulling in a charting dependency to draw
 * one line would be the kind of thing nobody removes later.
 */
function Sparkline({
  values: raw,
  label,
}: {
  values: number[];
  label: string;
}) {
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
  chart,
  unavailable,
  hero,
  href,
  linkLabel,
  askDraft,
  onAsk,
}: {
  icon: typeof DollarSign;
  label: string;
  value: string;
  caption: ReactNode;
  series?: number[];
  /** A pre-built chart (the shared RevenueBars) rendered instead of a Sparkline. */
  chart?: ReactNode;
  unavailable?: boolean;
  /** Spans two columns beside the tiles; renders `chart` (the hero bars). Revenue only. */
  hero?: boolean;
  /** In-shell destination; href stays real for new-tab / middle-click. */
  href?: string;
  linkLabel?: string;
  /** The question this tile's "Ask CEO" link hands to the CEO agent. */
  askDraft?: string;
  /** Hands `askDraft` to the CEO. One `useAskCeo()` call lives in the
   *  strip and is threaded down here — a tile calling the hook itself would
   *  mean one hook instance per tile for no reason. */
  onAsk?: (draft: string) => void;
}) {
  return (
    <div
      // `role="group"`, not a bare div: aria-label only names an element with
      // a role that supports naming, so without this the label is ignored by
      // the a11y tree (and `getByLabelText` only found it via the fallback).
      role="group"
      aria-label={label}
      data-hero={hero ? "true" : undefined}
      className={
        hero
          ? "rounded-md border border-[var(--edge)] bg-card px-5 py-4 @3xl:row-span-3"
          : "rounded-md border border-[var(--edge)] bg-card px-4 py-3"
      }
    >
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
            : hero
              ? "font-editorial-serif text-4xl @md:text-5xl tabular-nums leading-none text-[var(--midnight)]"
              : "font-editorial-serif text-2xl tabular-nums leading-none text-[var(--midnight)]"
        }
      >
        {unavailable ? "Unavailable" : value}
      </p>
      <p className="mt-1.5 text-xs text-[var(--midnight-muted)]">
        {unavailable ? "Not wired up yet" : caption}
      </p>
      {!unavailable &&
        (chart ?? (series && <Sparkline values={series} label={`${label} over the period`} />))}
      {href && linkLabel && (
        <a
          href={href}
          // Keeps the jump in-shell (preventDefault + pushState) while the
          // href stays a real URL for new-tab / middle-click.
          onClick={(e) => handleShellLinkClick(e, href)}
          className="mt-2.5 inline-block label-caps text-[var(--midnight)] underline-offset-4 hover:underline"
        >
          {linkLabel} →
        </a>
      )}
      {askDraft && !unavailable && (
        <button
          type="button"
          onClick={() => onAsk?.(askDraft)}
          className={`mt-2.5 inline-block label-caps text-[var(--midnight-muted)] underline-offset-4 hover:text-[var(--midnight)] hover:underline ${href && linkLabel ? "ml-4" : ""}`}
        >
          Ask CEO
        </button>
      )}
    </div>
  );
}

const SPANS: { value: string; label: string }[] = [
  { value: "7", label: "7d" },
  { value: "30", label: "30d" },
  { value: "90", label: "90d" },
];

/** "$40 in the previous 30 days" when the previous period is fully inside the
 *  readings; otherwise say there is nothing to compare against — never a zero
 *  for days that were never measured (there is no backfill).
 *
 *  The fallback deliberately does NOT name a date. `first_day` is the first row
 *  in the REQUESTED RANGE, not the owner's first ever reading (useCompanyRollup
 *  contract 3), so "Readings began <first_day>" was an origin claim the route
 *  never made. What we actually know is narrower and is what we now say. */
function comparison(
  prevSum: number | null,
  span: number,
  firstDay: string | null,
  fmt: (n: number) => string,
): string {
  if (prevSum !== null) return `${fmt(prevSum)} in the previous ${span} days`;
  return firstDay ? "No previous period to compare" : `No reading for the previous ${span} days`;
}

export function HomeRollupStrip({
  spendCents,
}: {
  /** Billed spend from `/teams/dashboard.costs`. THREE states, all distinct:
   *  a number is the figure, `null` is "the backend omitted the block", and
   *  `undefined` is "that request has not come back yet". */
  spendCents: number | null | undefined;
}) {
  const [span, setSpan] = useState<RollupSpan>(30);
  // One hook instance for the whole strip; every tile's Ask CEO button
  // hands its draft to this same `ask` rather than each calling the hook.
  const ask = useAskCeo();
  const { rollup, windowStart, isLoading, error, isUnavailable } = useCompanyRollup(span);
  // Deduped with Accounts' read of the same key. Only `details_submitted` is
  // consulted, and only when there are no rows — rows win (moneyView's rule).
  const { data: stripeStatus } = useCompanyStripeStatus();

  // `/company/rollup` calls require_org_admin, so an ordinary org member 403s
  // forever. Telling them it is temporary and to check back sends them to wait
  // for something that can never resolve — and SWR keeps retrying a request
  // that cannot succeed.
  if (error instanceof ApiError && error.status === 403) return null;

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
  if (!rollup && !isLoading) return null;

  // No Connect binding ⇒ no rollup rows exist at all, including the columns
  // that have nothing to do with Stripe. A silent set of zeros here would be a
  // lie: we do not know it is zero, we know we cannot see.
  if (rollup && !rollup.stripe_bound) {
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

  // The hook asks for 2 × span so the tail of the range IS the previous
  // period. Summing everything it returns under a "last 30 days" heading
  // would report sixty days as thirty, so the split is not optional.
  const all = rollup?.days ?? [];
  const days = all.filter((d) => d.day >= windowStart);
  const previous = all.filter((d) => d.day < windowStart);
  // What this ACTUALLY tests: a row exists on the very first day of the
  // doubled window. `first_day` is range-scoped (`rows[0].day` over the
  // requested range), so it can never precede `rollup.start` — the comparison
  // is an equality in all but name. It is a sound coverage test only because
  // the rollup writer is dense for a bound owner: a gap on exactly that first
  // day would read as "previous period not covered" and suppress an otherwise
  // honest comparison. Suppressing is the safe direction, so this stays.
  const previousCovered = !!rollup?.first_day && rollup.first_day <= rollup.start;
  const prev = (k: "revenue_microcents" | "signups" | "visitors") =>
    previousCovered ? sum(previous, k) : null;

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
  //
  // A COVERED previous period that had activity counts as something to report,
  // even when the current window is all zeros. $400 last month and $0 this one
  // is the single most important thing this strip can say, and collapsing threw
  // away the comparison caption that says it — leaving an owner whose revenue
  // stopped with the same sentence as an owner who never sold anything.
  const served = (["revenue_microcents", "signups", "visitors"] as const).filter(
    (k) => !isUnavailable(k),
  );
  // ANY non-zero day, not a zero SUM: revenue supports negative days, so a
  // sale and a refund that net to exactly zero across the window are two
  // real transactions, not "nothing happened".
  const anyReading = served.some((k) => days.some((d) => (d[k] ?? 0) !== 0));
  // Same test over the previous period, and only when it is covered — an
  // uncovered previous period has no comparison caption to preserve.
  const anyPreviousReading =
    previousCovered && served.some((k) => previous.some((d) => (d[k] ?? 0) !== 0));
  // Agent spend is part of "did anything happen". A pre-revenue company has
  // zero sales and real costs — collapsing to "nothing to report" would hide
  // the one number that owner most needs to see. `null` is "we cannot see it",
  // which is not the same as zero and must not keep the tiles alive.
  //
  // `undefined` is the THIRD state and the bug this guard exists for:
  // /dashboard is still in flight, so a rows-but-no-sales owner was told
  // "Nothing to report in the last 30 days" and then, a beat later, shown four
  // tiles. A verdict is not something to render from an unread request.
  const spendPending = spendCents === undefined;
  const spendKnown = spendCents !== null && spendCents !== undefined && spendCents !== 0;

  // Label the range from first_day, never from the window we asked for —
  // there is no backfill, so rollups start at the Stripe binding.
  const rangeLabel =
    rollup?.first_day && rollup.first_day > windowStart
      ? `Since ${formatShortDay(rollup.first_day)}`
      : `Last ${span} days`;
  // `days[]` is SPARSE — the writer returns None and stores nothing when every
  // source was empty, so absent days are omitted rather than zero-filled. The
  // sparkline plots ordinally, which means a gap of weeks looks identical to
  // consecutive days. Rather than pretend otherwise, the caption says how many
  // days actually carry a reading; plotting against the date is the real fix
  // and is deferred until the strip is worth a charting dependency.
  const readings = `${days.length} ${days.length === 1 ? "day" : "days"} with readings`;

  const prevRevenue = prev("revenue_microcents");

  let body: ReactNode;
  // A span change re-keys SWR, so `rollup` goes undefined mid-toggle. The
  // placeholder belongs INSIDE the section: hoisting it above cost the owner
  // the very tab strip they had just clicked, dropped focus to <body>, and on
  // a slow link left no way to change their mind. `keepPreviousData` is the
  // wrong fix — it would render the old span's rows under the new span's
  // windowStart, which is the bug this task exists to remove.
  if (!rollup) {
    body = <div className="h-[220px]" aria-hidden="true" />;
  }
  // Bound but unonboarded: Stripe returns legitimately empty for an account
  // with `details_submitted: false`, so "nothing happened yet" would send the
  // owner to wait for money that cannot arrive. Only consulted with no rows —
  // a reading beats a status flag.
  else if (!all.length && stripeStatus?.details_submitted === false) {
    body = (
      <PanelNotice
        state="partial"
        icon={DollarSign}
        headline="Finish your Stripe setup to start receiving payments."
        supporting="Your account is connected but Stripe hasn't finished onboarding it, so nothing can land here yet."
        href={teamsPanelUrl("accounts")}
        action={{
          label: "Finish setup",
          onClick: (e) => handleShellLinkClick(e, teamsPanelUrl("accounts")),
        }}
      />
    );
  } else if (!all.length) {
    // Bound minutes ago: `stripe_bound: true` with no rows, because the hourly
    // writer has not run and there is no backfill. Rendering the tiles here put
    // "$0" beside a range label reading "No readings yet" — two contradictory
    // statements on one strip, and three numbers we had not earned.
    body = (
      <PanelNotice
        state="zero"
        icon={DollarSign}
        headline="No readings yet."
        supporting="Your Stripe account is connected. The first daily reading lands within the hour."
      />
    );
  } else if (!anyReading && !anyPreviousReading && !spendKnown && !spendPending) {
    body = (
      <PanelNotice
        state="zero"
        icon={DollarSign}
        headline={`Nothing to report in the last ${span} days.`}
        supporting="Stripe is connected and reporting; there just hasn't been a sale or a signup in the window. The tiles fill in from the first one."
      />
    );
  } else {
    body = (
      // Container queries, not viewport breakpoints: this strip renders inside
      // a dock pane that is often half the viewport (a browser panel sits
      // beside it). With `lg:grid-cols-4`, a narrow pane still got four
      // columns and "Unavailable" truncated to "Unavailab". The container
      // is InlineTeamsPanel's wrapper, the same one DashboardPanel's own
      // `@md:`/`@4xl:` tiles resolve against.
      <div className="grid grid-cols-1 gap-2 @3xl:grid-cols-[2fr_1fr]">
        <Tile
          hero
          icon={DollarSign}
          label="Revenue"
          value={formatSignedUsdCents(microcentsToCents(sum(days, "revenue_microcents")))}
          caption={comparison(
            prevRevenue === null ? null : microcentsToCents(prevRevenue),
            span,
            rollup.first_day,
            formatSignedUsdCents,
          )}
          chart={<RevenueBars days={days} />}
          unavailable={isUnavailable("revenue_microcents")}
          href={teamsPanelUrl("payments")}
          linkLabel="Open Payments"
          askDraft={`Walk me through revenue for the last ${span} days: what landed, what was refunded, and what changed versus the previous ${span} days.`}
          onAsk={ask}
        />
        <Tile
          icon={UserPlus}
          label="New customers"
          value={formatCount(sum(days, "signups"))}
          caption={`Stripe customers created · ${comparison(prev("signups"), span, rollup.first_day, formatCount)}`}
          series={days.map((d) => d.signups)}
          unavailable={isUnavailable("signups")}
          href={teamsPanelUrl("customers")}
          linkLabel="Open Customers"
          askDraft={`Who are our new customers from the last ${span} days, and what did they buy?`}
          onAsk={ask}
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
          value={formatCount(sum(days, "visitors"))}
          caption={`From your PostHog · ${comparison(prev("visitors"), span, rollup.first_day, formatCount)}`}
          series={days.map((d) => d.visitors)}
          unavailable={isUnavailable("visitors")}
          href={isUnavailable("visitors") ? teamsPanelUrl("accounts") : undefined}
          linkLabel={isUnavailable("visitors") ? "Connect PostHog" : undefined}
          askDraft={`How is traffic to our site trending over the last ${span} days, and where is it coming from?`}
          onAsk={ask}
        />
        {/* Billed spend from Metronome via /teams/dashboard.costs — the figure
            Spend prints. `null` = the backend omitted the block (OAuth, BYOK,
            trial, non-admin member, or a resolver failure); "Not shown" is the
            honest word, never $0. No sparkline: it is a month scalar. */}
        {/* While /dashboard is in flight the value is an ellipsis and only this
            cell settles. The alternative — dropping the whole body back to the
            h-[220px] placeholder — would blank rollup figures we have ALREADY
            read to wait on a second, unrelated request, and flash box → tiles.
            One cell resolving is the calmer of the two. */}
        <Tile
          icon={Wallet}
          label="Agent spend"
          value={
            spendPending ? "…" : spendCents === null ? "Not shown" : formatUsdCents(spendCents)
          }
          caption={
            spendPending
              ? "Reading your bill"
              : spendCents === null
                ? "Open Spend for what your plan meters"
                : "Billed this month"
          }
          href={teamsPanelUrl("spend")}
          linkLabel="Open Spend"
          askDraft="Break down what my agents cost this month by agent and by task, and tell me what is driving it."
          onAsk={ask}
        />
      </div>
    );
  }

  return (
    <section aria-label="Business summary" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="label-caps text-[var(--midnight-muted)]">Business</p>
        <div className="flex items-center gap-3">
          {/* Two spans, not one interpolated string: the range and the
              reading count are separate facts, and a reader (or a test)
              looking for the range should not have to parse a separator
              out of the middle of a sentence. */}
          <p className="text-xs text-[var(--midnight-muted)]">
            <span>{rangeLabel}</span>
            {!!all.length && <> · {readings}</>}
          </p>
          <PanelTabs
            variant="pill"
            ariaLabel="Period"
            tabs={SPANS}
            value={String(span)}
            onValueChange={(v) => setSpan(Number(v) as RollupSpan)}
          />
        </div>
      </div>
      {body}

      {/* Deliberately NOT listing rollup.unavailable here. It is the fixed
          constant ["followers","open_threads"], so the line rendered for every
          owner forever — explaining the absence of two things this strip never
          offered a tile for. `isUnavailable` still guards each tile it does
          render, which is where the contract actually bites. */}
    </section>
  );
}
