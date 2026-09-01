"use client";

import { useCallback, useState } from "react";
import useSWR from "swr";
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
  | { kind: "unbound" }
  | { kind: "partial" }
  | { kind: "zero" }
  | { kind: "rows" };

export function moneyView<Row>(
  data: MoneyEnvelope<Row> | undefined,
  error: unknown,
  isLoading: boolean,
): MoneyView {
  if (error) return { kind: "unavailable" };
  if (isLoading || !data) return { kind: "loading" };
  if (!data.stripe_bound) return { kind: "unbound" };
  // `details_submitted` is absent on an older backend; absent must not read as
  // unonboarded, or every owner sees "finish setup" during a deploy skew.
  if (data.details_submitted === false) return { kind: "partial" };
  return data.data.length ? { kind: "rows" } : { kind: "zero" };
}

export function useCompanyMoney<Row>(kind: MoneyKind) {
  const api = useApi();
  const [cursor, setCursor] = useState<string | null>(null);

  const key = cursor ? `/company/${kind}?starting_after=${cursor}` : `/company/${kind}`;
  const { data, error, isLoading } = useSWR<MoneyEnvelope<Row>>(
    key,
    (path: string) => api.get(path) as Promise<MoneyEnvelope<Row>>,
    { dedupingInterval: 30_000 },
  );

  const next = useCallback(() => {
    if (data?.next_cursor) setCursor(data.next_cursor);
  }, [data?.next_cursor]);

  return {
    envelope: data,
    rows: data?.data ?? [],
    view: moneyView(data, error, isLoading),
    hasMore: !!data?.has_more,
    next,
    /** True once the owner has paged — the panel shows "showing later pages". */
    paged: cursor !== null,
    resetPaging: () => setCursor(null),
  };
}
