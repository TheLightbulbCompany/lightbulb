"use client";

// Money → Customers (`?panel=teams-customers`).
//
// The owner's question: who bought. The decision it informs: who to follow up
// with, and what Growth should do next.
//
// ATTRIBUTION IS DELIBERATELY ABSENT. Spec §4.4 puts "which campaign did this
// customer come from" in the OWNER's PostHog, joined on the Stripe customer's
// `metadata.user_id` — not in a join we build and hold. So this table shows
// who and when, and does not invent a source column it cannot source.

import type { PanelDataColumn } from "@/components/teams/shell";
import { MoneyPanel } from "@/components/teams/panels/money/MoneyPanel";
import type { CustomerRow } from "@/hooks/useCompanyMoney";
import { formatStripeAmount, formatUnixDay } from "@/components/teams/panels/money/money";

const COLUMNS: PanelDataColumn<CustomerRow>[] = [
  {
    key: "who",
    header: "Customer",
    cell: (r) => (
      <span className="min-w-0">
        {r.name || r.email || <span className="text-[var(--midnight-muted)]">Unnamed</span>}
        {r.name && r.email && (
          <span className="ml-2 text-xs text-[var(--midnight-muted)]">{r.email}</span>
        )}
      </span>
    ),
  },
  { key: "created", header: "Since", cell: (r) => formatUnixDay(r.created), width: "9rem" },
  {
    key: "standing",
    header: "Standing",
    width: "9rem",
    // `delinquent` is null on accounts Stripe has not evaluated. Null is not
    // "good standing" — it is unknown, and system.md says unknown renders
    // neutral, never ok.
    cell: (r) =>
      r.delinquent === true ? (
        <span className="text-warn">Past due</span>
      ) : r.delinquent === false ? (
        <span className="text-ok">Current</span>
      ) : (
        <span className="text-[var(--midnight-muted)]">—</span>
      ),
  },
  {
    key: "balance",
    header: "Balance",
    align: "right",
    width: "8rem",
    // Stripe's sign convention is the opposite of the intuitive one: a
    // NEGATIVE balance is credit the customer holds. Labelled rather than
    // rendered as a bare negative number that reads as debt.
    cell: (r) =>
      r.balance === 0 ? (
        <span className="text-[var(--midnight-muted)]">—</span>
      ) : (
        <span className="tabular-nums">
          {r.balance < 0
            ? `${formatStripeAmount(-r.balance, r.currency)} credit`
            : `${formatStripeAmount(r.balance, r.currency)} owed`}
        </span>
      ),
  },
];

export function CustomersPanel() {
  return <MoneyPanel<CustomerRow> kind="customers" title="Customers" noun="customers" columns={COLUMNS} maxWidth="max-w-5xl" />;
}
