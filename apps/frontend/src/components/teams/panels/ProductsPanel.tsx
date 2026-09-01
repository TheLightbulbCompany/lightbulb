"use client";

// Money → Products (`?panel=teams-products`).
//
// The owner's question: what am I selling, and at what price.
//
// This shipped as its own tab on the owner's ruling. The earlier proposal was
// to fold it into Payments as a section, on the reasoning that the Stripe
// Dashboard does it better and the real action ("raise the price") is a chat
// instruction. The data source settles it the other way: Payments, Products
// and Customers are three peer reads of one connected account, and a section
// would have made one of the three a second-class citizen of another's tab.
//
// The `default_price` may arrive expanded or as a bare id depending on what
// Stripe returns; both are handled rather than assuming the expand.

import { PanelDataTable, PanelMasthead, type PanelDataColumn } from "@/components/teams/shell";
import { EditorialButton } from "@/components/teams/shell";
import { MoneyStates } from "@/components/teams/panels/money/MoneyStates";
import { useCompanyMoney, type ProductRow } from "@/hooks/useCompanyMoney";
import { formatUsdCents } from "@/lib/creditAmounts";

function priceOf(p: ProductRow["default_price"]): string {
  if (!p) return "—";
  if (typeof p === "string") return "Set in Stripe";
  if (p.unit_amount === null || p.unit_amount === undefined) return "Variable";
  return p.currency?.toLowerCase() === "usd"
    ? formatUsdCents(p.unit_amount)
    : `${(p.unit_amount / 100).toFixed(2)} ${p.currency?.toUpperCase() ?? ""}`.trim();
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
    key: "active",
    header: "Status",
    width: "8rem",
    cell: (r) =>
      r.active ? (
        <span className="text-ok">Live</span>
      ) : (
        <span className="text-[var(--midnight-muted)]">Archived</span>
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

export function ProductsPanel() {
  const { rows, view, hasMore, next, paged } = useCompanyMoney<ProductRow>("products");

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead
        overline="Money"
        title="Products"
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
        <MoneyStates view={view} noun="products" />
      )}
    </div>
  );
}
