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

import { PanelDataTable, PanelMasthead, type PanelDataColumn } from "@/components/teams/shell";
import { EditorialButton } from "@/components/teams/shell";
import { MoneyStates } from "@/components/teams/panels/money/MoneyStates";
import { useCompanyMoney, type CustomerRow } from "@/hooks/useCompanyMoney";
import { formatUsdCents } from "@/lib/creditAmounts";

function when(unix: number): string {
  return new Date(unix * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

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
  { key: "created", header: "Since", cell: (r) => when(r.created), width: "9rem" },
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
            ? `${formatUsdCents(-r.balance)} credit`
            : `${formatUsdCents(r.balance)} owed`}
        </span>
      ),
  },
];

export function CustomersPanel() {
  const { rows, view, hasMore, next, paged } = useCompanyMoney<CustomerRow>("customers");

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-5xl mx-auto w-full">
      <PanelMasthead
        overline="Money"
        title="Customers"
        meta={paged ? "Later pages" : undefined}
      />

      {view.kind === "rows" ? (
        <>
          <PanelDataTable rows={rows} columns={COLUMNS} rowKey={(r) => r.id} />
          {hasMore && (
            <div className="flex justify-center">
              <EditorialButton size="sm" variant="secondary" onClick={next}>
                Load more
              </EditorialButton>
            </div>
          )}
        </>
      ) : view.kind === "loading" ? (
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={(r) => r.id} loading />
      ) : (
        <MoneyStates view={view} noun="customers" />
      )}
    </div>
  );
}
