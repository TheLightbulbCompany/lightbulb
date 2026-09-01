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
function Sparkline({ values, label }: { values: number[]; label: string }) {
  if (values.length < 2) return null;
  const max = Math.max(...values, 1);
  const step = 100 / (values.length - 1);
  const points = values
    .map((v, i) => `${(i * step).toFixed(2)},${(20 - (v / max) * 18).toFixed(2)}`)
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
      <p
        className={
          unavailable
            ? "font-editorial-serif text-lg italic leading-none text-[var(--midnight-muted)]"
            : "font-editorial-serif text-2xl tabular-nums leading-none text-[var(--midnight)]"
        }
      >
        {value}
      </p>
      <p className="mt-1.5 text-xs text-[var(--midnight-muted)]">{caption}</p>
      {series && !unavailable && <Sparkline values={series} label={`${label} over the period`} />}
    </div>
  );
}

export function HomeRollupStrip() {
  const { rollup, isLoading, error } = useCompanyRollup();

  if (isLoading) return null;

  // A failed read is NOT zero revenue. Say we could not look.
  if (error || !rollup) {
    return (
      <PanelNotice
        state="unavailable"
        icon={DollarSign}
        headline="We couldn't read your business numbers just now."
        supporting="This is a temporary read failure, not a zero. Check back shortly."
      />
    );
  }

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

  // Label the range from first_day, never from the window we asked for —
  // there is no backfill, so rollups start at the Stripe binding.
  const rangeLabel = rollup.first_day
    ? `Since ${new Date(rollup.first_day + "T00:00:00Z").toLocaleDateString(undefined, {
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

      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Tile
          icon={DollarSign}
          label="Revenue"
          value={formatUsdCents(microcentsToCents(sum(days, "revenue_microcents")))}
          caption={days.length ? `Across ${days.length} days` : "No readings yet"}
          series={days.map((d) => d.revenue_microcents)}
        />
        <Tile
          icon={UserPlus}
          label="Signups"
          value={sum(days, "signups").toLocaleString()}
          caption={days.length ? `Across ${days.length} days` : "No readings yet"}
          series={days.map((d) => d.signups)}
        />
        <Tile
          icon={Eye}
          label="Visitors"
          value={sum(days, "visitors").toLocaleString()}
          caption={days.length ? `Across ${days.length} days` : "No readings yet"}
          series={days.map((d) => d.visitors)}
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

      {/* Columns the backend told us it cannot serve. Named, not hidden — an
          owner who sees five tiles and expects seven should be told why. */}
      {rollup.unavailable?.length > 0 && (
        <p className="text-xs text-[var(--midnight-muted)]">
          {rollup.unavailable.includes("followers") && rollup.unavailable.includes("open_threads")
            ? "Followers and open threads aren't wired up yet."
            : `${rollup.unavailable.join(" and ")} aren't wired up yet.`}
        </p>
      )}
    </section>
  );
}
