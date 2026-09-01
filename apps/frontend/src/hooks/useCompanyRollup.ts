"use client";

import useSWR from "swr";
import { useApi } from "@/lib/api";

/**
 * `GET /api/v1/company/rollup` — the owner-day rollup behind Home's business
 * strip. Shape verified against dev, 2026-09-01.
 *
 * Three contracts here will produce a confidently wrong UI if you assume the
 * obvious reading, so they are typed and commented rather than trusted:
 *
 * 1. `agent_spend_month_to_date_microcents` is a TOP-LEVEL SCALAR and is
 *    deliberately absent from `days[]`. The writer stamps the running month
 *    total onto every current-month row, so summing a per-day column
 *    double-counts by roughly the number of days elapsed — and looks entirely
 *    plausible while doing it. There is no per-day spend series to plot.
 *
 * 2. `unavailable` names columns the backend knows it cannot serve
 *    (`followers`, `open_threads` are hardcoded 0 upstream: bundle.social is
 *    P3, and no owner-scoped thread store exists). They are omitted from
 *    `days[]` rather than sent as 0 — a 0 would render a tile that lies — and
 *    rather than null, which makes a client sum NaN. Render them
 *    `unavailable`, never empty.
 *
 * 3. `first_day` is when rollups BEGAN for this owner, which is when their
 *    Stripe account was bound. There is no backfill. A 30-day window whose
 *    `first_day` is three days ago genuinely under-reports, so label the range
 *    from `first_day` — never imply the window that was requested.
 *
 * And `stripe_bound: false` returns `200` with empty `days[]`, never a 404 or
 * 409. Empty-because-unbound and empty-because-nothing-happened are different
 * sentences to the owner, and this flag is the only thing that separates them.
 */
export interface RollupDay {
  day: string;
  revenue_microcents: number;
  signups: number;
  visitors: number;
}

export type RollupDirection = "in" | "out";

export interface CompanyRollup {
  start: string;
  end: string;
  stripe_bound: boolean;
  /** Null when nothing has ever been rolled up for this owner. */
  first_day: string | null;
  days: RollupDay[];
  agent_spend_month_to_date_microcents: number;
  /** Column names the backend cannot serve — render as unavailable, not 0. */
  unavailable: string[];
  /** Which way money flows, so a tile does not have to decide sign itself. */
  direction?: Record<string, RollupDirection>;
}

export function useCompanyRollup() {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<CompanyRollup>(
    "/company/rollup",
    (path: string) => api.get(path) as Promise<CompanyRollup>,
    {
      // The hourly writer is the only thing that moves these numbers, so a
      // tighter interval would just re-fetch an unchanged row on every Home
      // visit. Revalidating on focus covers the case where the owner leaves
      // the tab open across a write.
      dedupingInterval: 60_000,
    },
  );

  return {
    rollup: data,
    error,
    isLoading,
    refresh: mutate,
    /** A column the backend has told us it cannot serve. */
    isUnavailable: (key: string) => !!data?.unavailable?.includes(key),
  };
}
