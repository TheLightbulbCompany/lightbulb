"use client";

import { useCallback, useState } from "react";
import useSWR from "swr";
import { ApiError } from "@lightbulb/shared";
import { useApi } from "@/lib/api";

/**
 * The three Money list routes — `GET /api/v1/company/{payments,customers,products}`.
 * One hook because they share an envelope exactly; only `data`'s row shape differs.
 *
 * ── THE ENVELOPE IS THE STATE MACHINE ─────────────────────────────────────
 * These routes read the owner's connected Stripe account THROUGH, storing
 * nothing, and they answer 200 in every non-error case. Which empty state a
 * panel renders is decided entirely by three booleans, not by `data.length`:
 *
 *   stripe_bound: false          → `unbound`. No company Stripe account. The
 *                                  Stripe client is never even constructed.
 *                                  NOT a 404/409 — a 409 would turn every
 *                                  Money tab red for essentially the whole
 *                                  current fleet, none of whom have bound one.
 *   details_submitted: false     → `partial`. Bound but unonboarded, which
 *                                  returns legitimately empty lists. "Finish
 *                                  Stripe setup" is a DIFFERENT instruction
 *                                  from "no payments yet", and without this
 *                                  flag the two are indistinguishable.
 *   both true, data: []          → `zero`. Genuinely nothing in the range.
 *
 * A 502 (StripeError) is deliberately NOT swallowed to an empty list: a table
 * rendering "no payments" during a Stripe outage is a lie the owner would act
 * on. It surfaces as `unavailable`.
 *
 * ── PAGINATION IS STRIPE'S, NOT OURS ──────────────────────────────────────
 * `next_cursor` is Stripe's own `starting_after` passed straight through.
 * There is no total count and no server-side walk to build one — a page is a
 * view, never a copy, and the rollup day row stays the only sanctioned cache.
 */

export interface MoneyEnvelope<Row> {
  stripe_bound: boolean;
  data: Row[];
  has_more: boolean;
  next_cursor: string | null;
  details_submitted?: boolean;
  charges_enabled?: boolean;
  payouts_enabled?: boolean;
}

/** A Stripe **charge**, not a PaymentIntent — `receipt_url`, `refunded` and
 *  `amount_refunded` live on the charge, and reconciliation needs them. */
export interface PaymentRow {
  id: string;
  amount: number;
  amount_refunded: number;
  currency: string;
  created: number;
  status: string;
  paid: boolean;
  refunded: boolean;
  disputed: boolean;
  description: string | null;
  receipt_url: string | null;
  /** Left as an id rather than expanded — an expand costs a request per row. */
  customer: string | null;
}

export interface CustomerRow {
  id: string;
  email: string | null;
  name: string | null;
  created: number;
  currency: string | null;
  delinquent: boolean | null;
  balance: number;
}

export interface ProductRow {
  id: string;
  name: string;
  active: boolean;
  description: string | null;
  created: number;
  default_price: string | { id: string; unit_amount: number | null; currency: string } | null;
}

export type MoneyKind = "payments" | "customers" | "products";

/** What a Money panel should render, derived once so three panels cannot drift. */
export type MoneyView =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "forbidden" }
  | { kind: "unbound" }
  | { kind: "partial" }
  | { kind: "under_review" }
  | { kind: "zero" }
  | { kind: "rows" };

export function moneyView<Row>(
  data: MoneyEnvelope<Row> | undefined,
  error: unknown,
  isLoading: boolean,
): MoneyView {
  // A 403 is NOT a Stripe outage. `company_money._owner()` calls
  // `require_org_admin`, so every non-admin org member gets 403 on all four
  // routes — and collapsing that into "we couldn't reach Stripe, check back
  // shortly" tells them to wait for something that will never change. It is
  // permanent, it is ours, and it has a different sentence.
  if (error instanceof ApiError && error.status === 403) return { kind: "forbidden" };
  if (error) return { kind: "unavailable" };
  if (isLoading || !data) return { kind: "loading" };
  if (!data.stripe_bound) return { kind: "unbound" };
  // ROWS WIN. The onboarding booleans are only advanced by a best-effort
  // `_sync_status` at bind (which returns early on any StripeError) and by the
  // `account.updated` webhook, so they can be stale-false on an account that
  // has real charges — and an account Stripe later restricts keeps months of
  // history with `charges_enabled: false`. Checking them first hid the owner's
  // actual money behind "your onboarding isn't finished".
  if (data.data.length) return { kind: "rows" };
  // `details_submitted` is absent on an older backend; absent must not read as
  // unonboarded, or every owner sees "finish setup" during a deploy skew.
  if (data.details_submitted === false) return { kind: "partial" };
  // Submitted but not yet accepting charges: Stripe is reviewing the account.
  // This returns legitimately empty lists too, but telling the owner they are
  // "connected and ready" is false — no payment CAN land — and it contradicts
  // the Accounts panel, which renders this same state as "Under review".
  // `charges_enabled` is absent on an older backend, so only an explicit
  // `false` counts.
  if (data.charges_enabled === false) return { kind: "under_review" };
  return data.data.length ? { kind: "rows" } : { kind: "zero" };
}

export interface MoneyWindow {
  /** Inclusive UTC days, YYYY-MM-DD — the shape /company/payments takes. */
  start: string;
  end: string;
}

export function useCompanyMoney<Row>(kind: MoneyKind, window?: MoneyWindow) {
  const api = useApi();
  const [cursor, setCursor] = useState<string | null>(null);
  // Pages already fetched, in order. "Load more" APPENDS — swapping the SWR key
  // alone replaced the visible rows with the next page and left no way back to
  // page 1, and an empty final page then rendered "No payments yet" to an owner
  // who had just paged through dozens of them.
  const [earlier, setEarlier] = useState<Row[]>([]);
  // A window change is a different list: drop the pages accumulated under
  // the old one, or Sept 5's rows would sit above Sept 6's under a Sept 6
  // heading.
  const windowKey = window ? `${window.start}..${window.end}` : "";
  const [seenWindow, setSeenWindow] = useState(windowKey);
  if (seenWindow !== windowKey) {
    setSeenWindow(windowKey);
    setCursor(null);
    setEarlier([]);
  }

  const base = window
    ? `/company/${kind}?start=${window.start}&end=${window.end}`
    : `/company/${kind}`;
  const key = cursor ? `${base}${window ? "&" : "?"}starting_after=${cursor}` : base;
  const { data, error, isLoading } = useSWR<MoneyEnvelope<Row>>(
    key,
    (path: string) => api.get(path) as Promise<MoneyEnvelope<Row>>,
    // keepPreviousData only WHILE PAGING: without it, changing the cursor on
    // "Load more" blanks `data`, so the view flips to `loading` and every
    // accumulated row is replaced by a skeleton until the next page lands.
    // With it on a WINDOW change it is a lie — Sept 5's rows would sit under a
    // "Payments on Sept 6" heading until the new day lands. A window change
    // resets the cursor to null, so this is exactly the paging case.
    { dedupingInterval: 30_000, keepPreviousData: cursor !== null },
  );

  const next = useCallback(() => {
    if (!data?.next_cursor) return;
    setEarlier((prev) => [...prev, ...data.data]);
    setCursor(data.next_cursor);
  }, [data]);

  const rows = [...earlier, ...(data?.data ?? [])];
  const view = moneyView(data, error, isLoading);

  return {
    rows,
    // Once pages have accumulated the owner HAS rows, so no later page can take
    // them away — not an empty last page (`zero`), not a mid-flight fetch
    // (`loading`), and not a 502 on page 3 (`unavailable`), which would
    // otherwise strand them with no way back to page 1.
    view: rows.length && view.kind !== "forbidden" ? ({ kind: "rows" } as MoneyView) : view,
    hasMore: !!data?.has_more,
    /** Straight off the envelope — the only thing that can say an account is
     *  actually bound. A section may only offer a Stripe Dashboard link when
     *  there is a Stripe account behind it. */
    stripeBound: !!data?.stripe_bound,
    next,
  };
}
