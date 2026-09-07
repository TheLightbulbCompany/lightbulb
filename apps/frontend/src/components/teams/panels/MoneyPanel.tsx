"use client";

// Money — one ledger page (spec §3). No hero: Home has it. The masthead says
// what landed this month; the biggest day sits under it and links to that
// day's payments; then Payments, Customers, What you sell, Agent spend as
// sections. The four old tabs' keys open this page scrolled to their section.
//
// The Stripe sections are gated ONCE by the account state (unbound / partial /
// under review / forbidden / unavailable — MoneyStates), read off the Payments
// envelope. Agent spend is not Stripe's and always renders.

import { useEffect } from "react";
import { PanelMasthead } from "@/components/teams/shell";
import { useCompanyMoney, type PaymentRow } from "@/hooks/useCompanyMoney";
import { useCompanyRollup } from "@/hooks/useCompanyRollup";
import { handleShellLinkClick, teamsPanelUrl } from "@/lib/shellNav";
import { MoneyStates } from "./money/MoneyStates";
import { PaymentsSection } from "./money/PaymentsSection";
import { CustomersSection } from "./money/CustomersSection";
import { ProductsSection } from "./money/ProductsSection";
import { AgentSpendSection } from "./money/AgentSpendSection";
import { biggestDay, formatIsoDay, formatMicrocents, monthToDate } from "./money/money";

export type MoneySectionId = "payments" | "customers" | "products" | "spend";

const MONTH_NAMES = ["January","February","March","April","May","June","July","August","September","October","November","December"];

export function MoneyPanel({ section, day, onDayCleared }: { section?: MoneySectionId | null; day?: string | null; onDayCleared?: () => void }) {
  // The page-level gate reads the same envelope the Payments section reads
  // (same SWR key when unscoped), so it costs no extra request.
  const gate = useCompanyMoney<PaymentRow>("payments");
  const { rollup, isLoading: rollupLoading } = useCompanyRollup(90);

  const today = new Date().toISOString().slice(0, 10);
  const mtd = rollup?.stripe_bound && !rollupLoading ? monthToDate(rollup.days, today) : null;
  const best = mtd ? biggestDay(rollup!.days, mtd.month) : null;
  const monthName = mtd ? MONTH_NAMES[Number(mtd.month.slice(5, 7)) - 1] : null;
  // "New in 30 days" is copy, not the filter: it reuses the same month bucket
  // as the landed-revenue figure above rather than a separate rolling-30-day
  // scan, so the two numbers on this page are always drawn from one window.
  const newIn30Days = mtd
    ? rollup!.days.filter((d) => d.day.startsWith(mtd.month)).reduce((s, d) => s + (d.signups ?? 0), 0)
    : null;

  // The absorbed keys open this page at their section.
  useEffect(() => {
    if (!section) return;
    document.getElementById(`money-${section}`)?.scrollIntoView({ block: "start" });
  }, [section]);

  const showStripe = gate.view.kind === "rows" || gate.view.kind === "zero" || gate.view.kind === "loading";
  const dayHref = best ? `${teamsPanelUrl("money")}&day=${best.day}` : null;

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-6 max-w-5xl mx-auto w-full">
      <PanelMasthead overline="Money" title="Money" meta={mtd && monthName ? `${formatMicrocents(mtd.microcents)} landed in ${monthName}` : undefined} />
      {best && dayHref && (
        <p className="-mt-3 text-xs text-[var(--midnight-muted)]">
          Biggest day: {formatIsoDay(best.day)} · {formatMicrocents(best.revenue_microcents)} ·{" "}
          <a href={dayHref} onClick={(e) => handleShellLinkClick(e, dayHref)} className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Open that day</a>
        </p>
      )}

      {showStripe ? (
        <>
          <PaymentsSection day={day ?? null} onDayCleared={() => onDayCleared?.()} />
          <CustomersSection newIn30Days={newIn30Days} />
          <ProductsSection stripeBound={!!rollup?.stripe_bound} />
        </>
      ) : (
        <MoneyStates view={gate.view} noun="payments" />
      )}

      <AgentSpendSection />
    </div>
  );
}
