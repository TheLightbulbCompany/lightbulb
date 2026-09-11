"use client";

import { PanelDataTable, EditorialButton, type PanelDataColumn } from "@/components/teams/shell";
import { MoneySection, MoneyEmptyLine } from "./MoneySection";
import { useCompanyMoney, type PaymentRow } from "@/hooks/useCompanyMoney";
import { formatStripeAmount, formatUnixDay, formatIsoDay } from "@/components/teams/panels/money/money";

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
  { key: "created", header: "Date", cell: (r) => formatUnixDay(r.created), width: "8rem" },
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
        {formatStripeAmount(r.amount, r.currency)}
        {r.amount_refunded > 0 && !r.refunded && (
          <span className="ml-1 text-[var(--midnight-muted)]">
            (−{formatStripeAmount(r.amount_refunded, r.currency)})
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

/** Unscoped, this section is a PREVIEW, not a pager: the owner asked for a
 *  chart, and "a list of all of the payments doesn't help". Five rows answer
 *  "did anything land"; reconciling a ledger line by line is what the Stripe
 *  Dashboard is for, and the link goes there. A day scope is the exception —
 *  the owner asked for that day specifically, so it keeps the full list. */
const PREVIEW_ROWS = 5;

export function PaymentsSection({ day, onDayCleared }: { day: string | null; onDayCleared: () => void }) {
  const window = day ? { start: day, end: day } : undefined;
  const { rows, view, hasMore, next, stripeBound } = useCompanyMoney<PaymentRow>("payments", window);
  const rowKey = (r: PaymentRow) => r.id;
  const label = day ? `Payments on ${formatIsoDay(day)}` : "Payments";
  const visible = day ? rows : rows.slice(0, PREVIEW_ROWS);
  return (
    <MoneySection
      id="payments"
      label={label}
      right={
        day ? (
          <button type="button" onClick={onDayCleared} className="underline-offset-4 hover:text-[var(--midnight)] hover:underline">Show all payments</button>
        ) : (
          <>
            <span>{visible.length ? `${visible.length} most recent` : "newest first"}</span>
            {stripeBound && (
              <a
                href="https://dashboard.stripe.com/payments"
                target="_blank"
                rel="noopener noreferrer"
                className="underline-offset-4 hover:text-[var(--midnight)] hover:underline"
              >
                Full ledger in Stripe ↗
              </a>
            )}
          </>
        )
      }
    >
      {view.kind === "loading" ? (
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={rowKey} loading />
      ) : visible.length === 0 ? (
        <MoneyEmptyLine>{day ? `No payments on ${formatIsoDay(day)}.` : "No payments yet — the first one shows up here as soon as it lands."}</MoneyEmptyLine>
      ) : (
        <>
          <PanelDataTable rows={visible} columns={COLUMNS} rowKey={rowKey} />
          {day && hasMore && (
            <div className="flex justify-center">
              <EditorialButton size="sm" variant="secondary" onClick={next}>Show more</EditorialButton>
            </div>
          )}
        </>
      )}
    </MoneySection>
  );
}
