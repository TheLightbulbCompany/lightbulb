"use client";

import { PanelDataTable, EditorialButton, type PanelDataColumn } from "@/components/teams/shell";
import { MoneySection, MoneyEmptyLine } from "./MoneySection";
import { useCompanyMoney, type CustomerRow } from "@/hooks/useCompanyMoney";
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

export function CustomersSection({ newIn30Days }: { newIn30Days: number | null }) {
  const { rows, view, hasMore, next } = useCompanyMoney<CustomerRow>("customers");
  const rowKey = (r: CustomerRow) => r.id;
  return (
    <MoneySection
      id="customers"
      label="Customers"
      right={newIn30Days === null ? undefined : <span>{newIn30Days} new in 30 days</span>}
    >
      {view.kind === "loading" ? (
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={rowKey} loading />
      ) : rows.length === 0 ? (
        <MoneyEmptyLine>No customers yet.</MoneyEmptyLine>
      ) : (
        <>
          <PanelDataTable rows={rows} columns={COLUMNS} rowKey={rowKey} />
          {hasMore && (
            <div className="flex justify-center">
              <EditorialButton size="sm" variant="secondary" onClick={next}>Show more</EditorialButton>
            </div>
          )}
        </>
      )}
    </MoneySection>
  );
}
