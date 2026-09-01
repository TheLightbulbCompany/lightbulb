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
// There is deliberately no Status column: `stripe_page` sets `active=True` for
// the products kind, so every row IS live and the column could only ever print
// one value — 8rem of a table spent on zero information.

import { PanelDataTable, PanelMasthead, type PanelDataColumn } from "@/components/teams/shell";
import { EditorialButton } from "@/components/teams/shell";
import { MoneyStates } from "@/components/teams/panels/money/MoneyStates";
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

export function ProductsPanel() {
  const { rows, view, hasMore, next } = useCompanyMoney<ProductRow>("products");

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead
        overline="Money"
        title="Products"
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
