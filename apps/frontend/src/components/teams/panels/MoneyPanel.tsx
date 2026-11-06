"use client";

// Money — the revenue page. The masthead says what landed this month, then the
// revenue chart, which is the subject of the page (a trend the Stripe Dashboard
// does not show the owner), and under it Payments — a five-row preview, not a
// ledger — Customers, What you sell and Agent spend. The four old tabs' keys
// open this page scrolled to their section.
//
// There is deliberately no "Biggest day: … Open that day" line under the
// masthead any more: the tallest bar IS the biggest day and is already a link,
// so the sentence repeated the graph one line above it.
//
// The Stripe sections are gated ONCE by the account state (unbound / partial /
// under review / forbidden / unavailable — MoneyStates), read off the Payments
// envelope. Agent spend is not Stripe's and always renders.

import { useEffect, useRef } from "react";
import { PanelMasthead } from "@/components/teams/shell";
import { useCompanyMoney, type PaymentRow } from "@/hooks/useCompanyMoney";
import { useCompanyRollup, dayOffset } from "@/hooks/useCompanyRollup";
import { MoneyStates } from "./money/MoneyStates";
import { RevenueSection } from "./money/RevenueSection";
import { PaymentsSection } from "./money/PaymentsSection";
import { CustomersSection } from "./money/CustomersSection";
import { ProductsSection } from "./money/ProductsSection";
import { AgentSpendSection } from "./money/AgentSpendSection";
import { formatMicrocents, monthToDate } from "./money/money";

export type MoneySectionId = "payments" | "customers" | "products" | "spend";

const MONTH_NAMES = ["January","February","March","April","May","June","July","August","September","October","November","December"];

/** `embedded`: inside Settings › Business (super-agent), whose column and title it sits under: no masthead of its own,
 *  no page width. The month's figure stays, as the first line. */
export function MoneyPanel({ section, day, onDayCleared, embedded = false }: { section?: MoneySectionId | null; day?: string | null; onDayCleared?: () => void; embedded?: boolean }) {
  // The page-level gate reads the same envelope the Payments section reads
  // (same SWR key when unscoped), so it costs no extra request.
  const gate = useCompanyMoney<PaymentRow>("payments");
  const { rollup, isLoading: rollupLoading } = useCompanyRollup(90);

  const today = new Date().toISOString().slice(0, 10);
  const mtd = rollup?.stripe_bound && !rollupLoading ? monthToDate(rollup.days, today) : null;
  const monthName = mtd ? MONTH_NAMES[Number(mtd.month.slice(5, 7)) - 1] : null;
  // `first_day === null` means the rollup has never read anything for this
  // owner, so 0 signups is "we don't know", not "nobody signed up".
  const newIn30Days = rollup?.stripe_bound && !rollupLoading && rollup.first_day !== null
    ? rollup.days.filter((d) => d.day >= dayOffset(today, -29)).reduce((s, d) => s + (d.signups ?? 0), 0)
    : null;

  // The absorbed keys open this page at their section. Scoped to this page's
  // root (a dock layout can hold two panels with the same ids) and re-run once
  // the gate resolves — on the first pass the sections below are still a
  // skeleton, so the anchor does not exist yet.
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!section) return;
    rootRef.current?.querySelector(`#money-${section}`)?.scrollIntoView?.({ block: "start" });
  }, [section, gate.view.kind]);

  const showStripe = gate.view.kind === "rows" || gate.view.kind === "zero" || gate.view.kind === "loading";

  return (
    <div ref={rootRef} className={embedded ? "flex flex-col gap-6 w-full" : "editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-6 max-w-5xl mx-auto w-full"}>
      {embedded
        ? mtd && monthName && <p className="text-ui text-muted-foreground tabular-nums">{`${formatMicrocents(mtd.microcents)} ${mtd.microcents < 0 ? "net" : "landed"} in ${monthName}`}</p>
        : <PanelMasthead overline="Money" title="Money" meta={mtd && monthName ? `${formatMicrocents(mtd.microcents)} ${mtd.microcents < 0 ? "net" : "landed"} in ${monthName}` : undefined} />}

      {showStripe ? (
        <>
          <RevenueSection day={day ?? null} onDayCleared={() => onDayCleared?.()} />
          <PaymentsSection day={day ?? null} onDayCleared={() => onDayCleared?.()} />
          <CustomersSection newIn30Days={newIn30Days} />
          <ProductsSection stripeBound={!!rollup?.stripe_bound} />
        </>
      ) : (
        <MoneyStates view={gate.view} noun="payments" />
      )}

      {/* Both Money routes and /billing require org admin, so a member who
          403s on payments would 403 here too — one notice, not two. */}
      {gate.view.kind !== "forbidden" && <AgentSpendSection />}
    </div>
  );
}
