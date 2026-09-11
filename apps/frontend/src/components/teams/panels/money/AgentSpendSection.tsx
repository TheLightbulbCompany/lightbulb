"use client";

// apps/frontend/src/components/teams/panels/money/AgentSpendSection.tsx
//
// Money ledger → Agent spend section. The owner's five-second question: what
// are my agents costing me this period, and is anything running away.
//
// This is the Spend tab's headline, lifted into a `MoneySection` — no
// masthead, no sub-tab. The token/cache grid and the per-member table are
// deliberately left on the control Usage panel (`?panel=usage`, Settings) —
// see spec §4 rule 4. This section renders only the billed figure, the run
// count, and the by-model / plan-window rows.
//
// ── WHICH FIGURES ARE REAL ────────────────────────────────────────────────
// This panel shows no number it cannot source. An owner makes a spending
// decision here, so a confident wrong number is worse than no number.
//
//  LIVE      `billed_month_to_date_cents` from GET /billing/spend in
//            `bedrock` mode. Read off the Metronome DRAFT INVOICE (#1236) —
//            the ledger that actually bills — so it is the figure charged.
//  LIVE      request count + period label from GET /billing/usage, and the
//            per-member split (org admins only; personal owners get []).
//  NEVER     /billing/usage's `total_spend` and `lifetime_spend` AS FIGURES.
//            Those are the DynamoDB usage counters, which are analytics-only
//            (CLAUDE.md "Usage Flow"), and #1236 deliberately pulled them out
//            of the UI. UsagePanel.spend.test.tsx pins their absence; do not
//            reintroduce them here just because this tab is named after money.
//            `lifetime_spend` is read in ONE place below — as a boolean, to
//            tell a month rollover apart from a company that has never run
//            anything. No number derived from it is ever printed.
//  PARTIAL   `tokens_only` is ambiguous by construction:
//            spend_service.get_spend_summary returns it BOTH for owners with
//            no platform-billed inference (BYOK, free tier) AND as the
//            degraded fallback when the invoice/quota read fails. The two are
//            told apart here by the owner's stored `provider_choice`, and a
//            degraded read is labelled "Unavailable" rather than rendered as
//            a confident $0.
//
// NO TREND, DELIBERATELY. The only owner-day time series in the product is
// P0c's rollup table, which has no HTTP read route at all and whose writers
// only iterate owners with a bound company Stripe account — a sparkline off
// it would be empty for essentially every owner. /billing/usage carries one
// period, so one period is what this renders.

import Link from "next/link";
import useSWR from "swr";
import { useBilling, type UsageSummary } from "@/hooks/useBilling";
import { useSpend, type SpendResponse } from "@/hooks/useSpend";
import { formatUsdCents } from "@/lib/creditAmounts";
import { MoneySection, MoneyEmptyLine } from "./MoneySection";

interface Headline {
  figure: string;
  /** true when the figure is a dollar amount (gets the big tabular treatment). */
  money: boolean;
  caption: string;
  /** Renders the "Partial" chip — the figure is missing or unverified, not zero. */
  partial: boolean;
}

/** The one honest sentence about this owner's billed spend, chosen from what
 *  /billing/spend and the billing row can actually prove. `undefined` while
 *  /billing/spend is still in flight — a placeholder zero would be a wrong
 *  number in the window before it lands. */
export function spendHeadline(
  spend: SpendResponse | undefined,
  providerChoice: string | null | undefined,
  subscriptionStatus: string | null | undefined,
): Headline | null {
  if (!spend) return null;

  if (spend.mode === "bedrock" && spend.billed_month_to_date_cents !== null) {
    return {
      figure: formatUsdCents(spend.billed_month_to_date_cents ?? 0),
      money: true,
      // Deliberately NOT the same sentence as the "Billed this month" card in
      // the body below — one page, two phrasings of one number reads as two
      // numbers. This one names what the figure is; the card names its source.
      caption: "Billed so far this period — the invoiced amount, not an estimate from token counts.",
      partial: false,
    };
  }

  if (spend.mode === "oauth_quota") {
    return {
      figure: "Included",
      money: false,
      caption:
        "Your agents run on your ChatGPT plan, so we are not billed for their inference. The plan windows below are the meter.",
      partial: false,
    };
  }

  // Everything past here is `tokens_only`, which carries no dollars at all.
  if (subscriptionStatus === "trial_local") {
    return {
      figure: "No charge",
      money: false,
      caption: "You are on the free tier — agent runs are not billed. The volume below is what we measured.",
      partial: false,
    };
  }
  if (providerChoice === "byo_key") {
    return {
      figure: "Not billed by us",
      money: false,
      caption:
        "You run on your own API key, so the charges are on your provider's invoice and we cannot see them. The volume below is what we measured.",
      partial: false,
    };
  }
  if (providerChoice === "bedrock_claude" || providerChoice === "chatgpt_oauth") {
    return {
      figure: "Unavailable",
      money: false,
      caption:
        "We could not read your billed spend just now. This is a temporary read failure, not a zero-dollar period — check back shortly.",
      partial: true,
    };
  }
  // No stored provider (legacy row, or a billing row that has not landed yet):
  // both "nothing to bill" and "the read failed" reach here, and the frontend
  // cannot tell which. Say exactly that.
  return {
    figure: "Not shown",
    money: false,
    caption:
      "No billed figure for this period — either your plan does not bill per run, or we could not read the invoice. The volume below is what we measured.",
    partial: true,
  };
}

/** "2026-09" -> "September". null for anything that isn't a period label.
 *
 *  The label comes from /billing/usage, i.e. the server's UTC month. The
 *  browser clock is a second and sometimes disagreeing source of truth for the
 *  same thing (a viewer in UTC-7 is still on August 31 when the period rolls),
 *  so it gets no vote here — which is also why the copy below names the month
 *  rather than how many days into it we are. */
function periodMonthName(period: string | undefined): string | null {
  const match = /^(\d{4})-(\d{2})$/.exec(period ?? "");
  if (!match) return null;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return null;
  return new Date(Date.UTC(Number(match[1]), month - 1, 1)).toLocaleString("en-US", {
    month: "long",
    timeZone: "UTC",
  });
}

export function AgentSpendSection() {
  const { account, fetchUsage } = useBilling();
  const { spend } = useSpend();
  // Same SWR key UsagePanel uses, so this reads the shared cache entry rather
  // than issuing a second /billing/usage request.
  const { data: usage, isLoading } = useSWR<UsageSummary | null>("/billing/usage", () => fetchUsage(), { dedupingInterval: 30000 });

  const headline = spendHeadline(spend, account?.provider_choice, account?.subscription_status);
  const billedCents = spend?.mode === "bedrock" ? (spend.billed_month_to_date_cents ?? 0) : 0;
  // "Nothing has happened yet" is a claim about the company, so it needs both
  // halves to be true: no runs counted AND nothing billed. A non-zero invoice
  // with a lagging counter must never render as "no agent runs".
  const nothingThisPeriod = !isLoading && (!usage || usage.request_count === 0) && billedCents === 0;
  // "Period" is the calendar month, so on the 1st every active company's
  // counters read zero. Name the rollover instead of implying the fleet is
  // dead — see SpendPanel's original comment (git history) for the full case.
  const ranInAnEarlierPeriod = (usage?.lifetime_spend ?? 0) > 0;
  const monthName = periodMonthName(usage?.period);

  const right = (
    <>
      {usage?.period && <span>{periodMonthName(usage.period) ?? usage.period}</span>}
      <Link href="/chat?panel=llm" className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Manage credits</Link>
    </>
  );

  return (
    <MoneySection id="spend" label="Agent spend" right={right}>
      {nothingThisPeriod ? (
        <MoneyEmptyLine>
          {ranInAnEarlierPeriod && monthName
            ? `No agent runs yet in ${monthName}. Your agents ran in an earlier period; ${monthName} counts from zero.`
            : "No agent runs this period. Nothing has been billed because nothing has run yet."}
        </MoneyEmptyLine>
      ) : (
        <div className="flex flex-col gap-3 px-2 py-2">
          {headline && (
            <div data-testid="spend-headline" className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="label-caps">This period</p>
                  {headline.partial && <span className="label-caps rounded-sm border border-[var(--edge)] px-1.5 py-0.5 text-[var(--midnight-muted)]">Partial</span>}
                </div>
                <p className={headline.money ? "font-editorial-serif text-3xl leading-none tabular-nums text-[var(--midnight)]" : "font-editorial-serif text-xl italic leading-tight text-[var(--midnight)]"}>{headline.figure}</p>
                <p className="mt-1 max-w-[52ch] text-xs text-[var(--midnight-muted)]">{headline.caption}</p>
              </div>
              {usage && (
                <div className="text-right">
                  <p className="label-caps">Agent runs</p>
                  <p className="font-mono text-title tabular-nums text-[var(--midnight)]">{usage.request_count.toLocaleString()}</p>
                </div>
              )}
            </div>
          )}
          {spend?.mode === "bedrock" && (spend.by_model ?? []).length > 0 && (
            <dl className="flex flex-col divide-y divide-[var(--edge-soft)] border-t border-[var(--edge-soft)]">
              {(spend.by_model ?? []).map((row) => (
                <div key={row.tier} className="flex items-center justify-between py-1.5 text-sm">
                  <dt>{row.model}</dt>
                  <dd className="font-mono text-xs tabular-nums text-[var(--midnight-muted)]">{formatUsdCents(row.cents)}</dd>
                </div>
              ))}
            </dl>
          )}
          {spend?.mode === "oauth_quota" && (spend.windows ?? []).map((w) => (
            <div key={w.label} className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span>{w.label} limit</span>
                <span className="text-[var(--midnight-muted)]">{w.used_percent > 0 && w.used_percent < 1 ? "<1" : Math.round(w.used_percent)}% used{w.resets_at_ms ? ` · resets ${new Date(w.resets_at_ms).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}` : ""}</span>
              </div>
              <div className="h-1.5 rounded-sm bg-[var(--edge)]"><div className="h-1.5 rounded-sm bg-[var(--midnight)]" style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%` }} /></div>
            </div>
          ))}
        </div>
      )}
    </MoneySection>
  );
}
