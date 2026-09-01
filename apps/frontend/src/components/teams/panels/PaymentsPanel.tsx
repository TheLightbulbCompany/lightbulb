"use client";

// Money → Payments (`?panel=teams-payments`).
//
// The owner's question: did the money we think we made actually land. The
// decision it informs: chase a failed charge, or stop worrying. That is a
// scanning task over many rows, which is why it is a tab and not a question
// for Friday.
//
// TRUST COMES FROM RECONCILABILITY. Every row carries its Stripe receipt, so
// the owner can match this table against their Stripe Dashboard line for line.
// A money figure the owner cannot trace to the provider is worse than no
// figure — they cannot act on it, and finding out it was wrong costs us the
// whole surface.
//
// These are **charges**, not PaymentIntents: `receipt_url`, `refunded` and
// `amount_refunded` live on the charge, and reconciliation needs all three.

import { PanelDataTable, PanelMasthead, type PanelDataColumn } from "@/components/teams/shell";
import { EditorialButton } from "@/components/teams/shell";
import { MoneyStates } from "@/components/teams/panels/money/MoneyStates";
import { useCompanyMoney, type PaymentRow } from "@/hooks/useCompanyMoney";
import { formatUsdCents } from "@/lib/creditAmounts";

function when(unix: number): string {
  return new Date(unix * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/** Stripe amounts are minor units. USD minor units are cents, so
 *  `formatUsdCents` is correct; a zero-decimal currency (JPY) would not be,
 *  which is why the currency is shown next to a non-USD amount. */
function money(amount: number, currency: string): string {
  if (currency?.toLowerCase() === "usd") return formatUsdCents(amount);
  return `${(amount / 100).toFixed(2)} ${currency?.toUpperCase() ?? ""}`.trim();
}

/** Never colour alone (system.md) — the word carries the status, the colour
 *  only reinforces it, and an unknown status renders neutral rather than ok. */
function statusOf(row: PaymentRow): { label: string; tone: string } {
  if (row.disputed) return { label: "Disputed", tone: "text-err" };
  if (row.refunded) return { label: "Refunded", tone: "text-[var(--midnight-muted)]" };
  if (row.amount_refunded > 0) return { label: "Partly refunded", tone: "text-warn" };
  if (row.paid && row.status === "succeeded") return { label: "Paid", tone: "text-ok" };
  if (row.status === "failed") return { label: "Failed", tone: "text-err" };
  return { label: row.status || "Unknown", tone: "text-[var(--midnight-muted)]" };
}

const COLUMNS: PanelDataColumn<PaymentRow>[] = [
  { key: "created", header: "Date", cell: (r) => when(r.created), width: "8rem" },
  {
    key: "description",
    header: "Description",
    cell: (r) => r.description || <span className="text-[var(--midnight-muted)]">—</span>,
  },
  {
    key: "status",
    header: "Status",
    cell: (r) => {
      const s = statusOf(r);
      return <span className={s.tone}>{s.label}</span>;
    },
    width: "9rem",
  },
  {
    key: "amount",
    header: "Amount",
    align: "right",
    cell: (r) => (
      <span className="tabular-nums">
        {money(r.amount, r.currency)}
        {r.amount_refunded > 0 && !r.refunded && (
          <span className="ml-1 text-[var(--midnight-muted)]">
            (−{money(r.amount_refunded, r.currency)})
          </span>
        )}
      </span>
    ),
    width: "10rem",
  },
  {
    key: "receipt",
    header: "",
    align: "right",
    width: "5rem",
    cell: (r) =>
      r.receipt_url ? (
        <a
          href={r.receipt_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs underline text-[var(--midnight)]"
        >
          Receipt
        </a>
      ) : null,
  },
];

export function PaymentsPanel() {
  const { rows, view, hasMore, next, paged } = useCompanyMoney<PaymentRow>("payments");

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-5xl mx-auto w-full">
      <PanelMasthead
        overline="Money"
        title="Payments"
        meta={paged ? "Later pages" : undefined}
      />

      {view.kind === "rows" ? (
        <>
          <PanelDataTable rows={rows} columns={COLUMNS} rowKey={(r) => r.id} />
          {/* Stripe's own cursor. There is deliberately no page count: getting
              one means walking every page server-side, and a page is a view,
              not a copy. */}
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
        <MoneyStates view={view} noun="payments" />
      )}
    </div>
  );
}
