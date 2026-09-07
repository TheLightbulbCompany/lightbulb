"use client";

import { PanelDataTable, EditorialButton, type PanelDataColumn } from "@/components/teams/shell";
import { MoneySection, MoneyEmptyLine } from "./MoneySection";
import { useCompanyMoney, type ProductRow } from "@/hooks/useCompanyMoney";
import { formatStripeAmount } from "@/components/teams/panels/money/money";

function priceOf(p: ProductRow["default_price"]): string {
  if (!p) return "—";
  // The backend always expands `default_price`, so the bare-id shape does not
  // reach us — narrow it out rather than carrying a branch for a contract the
  // server does not offer.
  if (typeof p === "string") return "Set in Stripe";
  if (p.unit_amount === null || p.unit_amount === undefined) return "Variable";
  return formatStripeAmount(p.unit_amount, p.currency);
}

const COLUMNS: PanelDataColumn<ProductRow>[] = [
  {
    key: "name",
    header: "Product",
    cell: (r) => (
      <span>
        {r.name}
        {r.description && (
          <span className="ml-2 text-xs text-[var(--midnight-muted)]">{r.description}</span>
        )}
      </span>
    ),
  },
  {
    key: "price",
    header: "Price",
    align: "right",
    width: "9rem",
    cell: (r) => <span className="tabular-nums">{priceOf(r.default_price)}</span>,
  },
];

export function ProductsSection({ stripeBound }: { stripeBound: boolean }) {
  const { rows, view, hasMore, next } = useCompanyMoney<ProductRow>("products");
  const rowKey = (r: ProductRow) => r.id;
  return (
    <MoneySection
      id="products"
      label="What you sell"
      right={
        stripeBound ? (
          <a
            href="https://dashboard.stripe.com/products/create"
            target="_blank"
            rel="noopener noreferrer"
            className="underline-offset-4 hover:text-[var(--midnight)] hover:underline"
          >
            Add a product ↗
          </a>
        ) : undefined
      }
    >
      {view.kind === "loading" ? (
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={rowKey} loading />
      ) : rows.length === 0 ? (
        <MoneyEmptyLine>Nothing listed yet.</MoneyEmptyLine>
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
