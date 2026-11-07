"use client";

// apps/frontend/src/components/teams/panels/money/AgentSpendSection.tsx
//
// Money ledger → Agent spend section. The owner's five-second question: what
// are my agents costing me this period, and is anything running away.
//
// This is the Spend tab's headline, lifted into a `MoneySection` — no
// masthead, no sub-tab. The token/cache grid and the per-member table are
// deliberately left on the control Usage panel (`?panel=usage`, Settings) —
// see spec §4 rule 4. This section renders only the headline sentence and
// the run count.
//
// ── WHICH FIGURES ARE REAL ────────────────────────────────────────────────
// This panel shows no number it cannot source. An owner makes a spending
// decision here, so a confident wrong number is worse than no number.
//
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
//  NO DOLLAR FIGURE  GET /billing/spend was retired with backend Bedrock
//            billing (see CLAUDE.md "Free tier"/"Paid provider paths") — it
//            now carries no billed amount at all, so this section no longer
//            reads it. The headline is a plan-derived sentence (free tier /
//            BYOK / ChatGPT OAuth / not shown), never a number.
//
// NO TREND, DELIBERATELY. The only owner-day time series in the product is
// P0c's rollup table, which has no HTTP read route at all and whose writers
// only iterate owners with a bound company Stripe account — a sparkline off
// it would be empty for essentially every owner. /billing/usage carries one
// period, so one period is what this renders.

import Link from "next/link";
import useSWR from "swr";
import { useBilling, type UsageSummary } from "@/hooks/useBilling";
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
 *  the billing row can actually prove. There is no dollar-figure source left
 *  on this page (see the header comment), so every branch is a sentence
 *  about the plan, never a number. */
export function spendHeadline(
  providerChoice: string | null | undefined,
  subscriptionStatus: string | null | undefined,
): Headline {
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
  if (providerChoice === "chatgpt_oauth") {
    return {
      figure: "Not billed by us",
      money: false,
      caption:
        "Your agents run on your ChatGPT plan, so we are not billed for their inference. The volume below is what we measured.",
      partial: false,
    };
  }
  // Every other case (managed OpenRouter included, plus a legacy/unknown
  // row) has no billed figure left to show on this page.
  return {
    figure: "Not shown",
    money: false,
    caption: "We don't show a billed figure here. The volume below is what we measured.",
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
  const { account, fetchUsage, isLoading: accountLoading } = useBilling();
  // Super-agent: one type voice (Satoshi, sentence case; no caps, italic or mono), dev walk W12-14.
  const label = "text-ui text-[var(--midnight-muted)]";
  // Same SWR key UsagePanel uses, so this reads the shared cache entry rather
  // than issuing a second /billing/usage request.
  const { data: usage, isLoading: usageLoading } = useSWR<UsageSummary | null>("/billing/usage", () => fetchUsage(), { dedupingInterval: 30000 });

  // No headline until both the account and the usage row have loaded — a
  // placeholder sentence would be a wrong claim in that window.
  const headline = accountLoading || usageLoading ? null : spendHeadline(account?.provider_choice, account?.subscription_status);
  // "Nothing has happened yet" is a claim about the company, so it waits for
  // usage to load before deciding.
  const nothingThisPeriod = !usageLoading && (!usage || usage.request_count === 0);
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
                  <p className={label}>This period</p>
                  {headline.partial && <span className={`text-caption rounded-sm border border-[var(--edge)] px-1.5 py-0.5 text-[var(--midnight-muted)]`}>Partial</span>}
                </div>
                <p className={`font-semibold text-[var(--midnight)] ${headline.money ? "text-3xl leading-none tabular-nums" : "text-xl leading-tight"}`}>{headline.figure}</p>
                <p className="mt-1 max-w-[52ch] text-caption text-[var(--midnight-muted)]">{headline.caption}</p>
              </div>
              {usage && (
                <div className="text-right">
                  <p className={label}>Agent runs</p>
                  <p className={`text-title tabular-nums text-[var(--midnight)]`}>{usage.request_count.toLocaleString()}</p>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </MoneySection>
  );
}
