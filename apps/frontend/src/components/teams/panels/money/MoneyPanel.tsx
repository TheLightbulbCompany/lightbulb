"use client";

// The scaffold Payments, Products and Customers share: masthead, the table,
// the state notice, and "Load more". Extracted because the three were a
// verbatim 25-line copy modulo the column array — and the drift had already
// begun (two different max-widths). The same argument that justifies
// MoneyStates justifies this.
//
// ── KEEP THE INSTRUMENT ───────────────────────────────────────────────────
// The first dev walk showed the `partial` notice replacing the whole panel:
// masthead, notice, empty space. An owner arriving before their Stripe
// onboarding is finished never learned that Payments IS a table with Date,
// Status and Amount columns. So the column headers render UNDER the notice
// for every state where a table is coming — the notice explains why the body
// is empty, and the headers show what will fill it. `PanelDataTable` with no
// rows and no `empty` slot renders exactly that: headers, no body.
//
// Not for `forbidden` (there is no table for that person) and not while
// loading (the skeleton is the shape there).

import { PanelDataTable, PanelMasthead, EditorialButton, type PanelDataColumn } from "@/components/teams/shell";
import { MoneyStates } from "./MoneyStates";
import { useCompanyMoney, type MoneyKind } from "@/hooks/useCompanyMoney";

export function MoneyPanel<Row extends { id: string }>({
  kind,
  title,
  noun,
  columns,
  maxWidth = "max-w-5xl",
}: {
  kind: MoneyKind;
  title: string;
  /** Plural noun for the state copy — "payments", "customers", "products". */
  noun: string;
  columns: PanelDataColumn<Row>[];
  maxWidth?: string;
}) {
  const { rows, view, hasMore, next } = useCompanyMoney<Row>(kind);
  const rowKey = (r: Row) => r.id;

  return (
    <div className={`editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 ${maxWidth} mx-auto w-full`}>
      <PanelMasthead overline="Money" title={title} />

      {view.kind === "rows" ? (
        <>
          <PanelDataTable rows={rows} columns={columns} rowKey={rowKey} />
          {/* Stripe's own cursor. Deliberately no page count: getting one
              means walking every page server-side, and a page is a view,
              never a copy. */}
          {hasMore && (
            <div className="flex justify-center">
              <EditorialButton size="sm" variant="secondary" onClick={next}>
                Load more
              </EditorialButton>
            </div>
          )}
        </>
      ) : view.kind === "loading" ? (
        <PanelDataTable rows={[]} columns={columns} rowKey={rowKey} loading />
      ) : (
        <>
          <MoneyStates view={view} noun={noun} />
          {view.kind !== "forbidden" && (
            <PanelDataTable rows={[]} columns={columns} rowKey={rowKey} />
          )}
        </>
      )}
    </div>
  );
}
