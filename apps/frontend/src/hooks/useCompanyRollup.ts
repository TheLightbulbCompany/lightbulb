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
 *    It is not rendered anywhere: Home's spend tile reads the Metronome-billed
 *    figure in `/teams/dashboard.costs` (the DDB counter was removed from the
 *    Spend tab's UI too), so this field is served for API completeness only.
 *
 * 2. `unavailable` names columns the backend knows it cannot serve
 *    (`followers`, `open_threads` are hardcoded 0 upstream: bundle.social is
 *    P3, and no owner-scoped thread store exists). They are omitted from
 *    `days[]` rather than sent as 0 — a 0 would render a tile that lies — and
 *    rather than null, which makes a client sum NaN. Render them
 *    `unavailable`, never empty.
 *
 * 3. `first_day` is the first STORED ROW INSIDE THE REQUESTED RANGE —
 *    `rows[0]["day"]` over `[start, end]` (routers/company_money.py). It is
 *    range-scoped, not owner-scoped: it can never precede `start`, and the
 *    route serves no owner-level origin at all. So it bounds the RANGE LABEL
 *    honestly ("Since Aug 30" is true of the window shown) and nothing more.
 *    Do not read it as "readings began here" — for an owner bound months ago
 *    it is simply the oldest row the window reached. A genuinely owner-scoped
 *    first day would need a new backend field; out of scope here.
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

/** The Business section's period toggle. 30 is the route's own default
 *  (company_money.DEFAULT_SPAN_DAYS). */
export type RollupSpan = 7 | 30 | 90;

/** UTC day arithmetic on YYYY-MM-DD. `Date.UTC` so a viewer in UTC-7 on the
 *  evening of the 31st still computes the same window the server will label. */
export function dayOffset(day: string, delta: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + delta)).toISOString().slice(0, 10);
}

/**
 * Request TWICE the span: rows before `windowStart` are the previous period,
 * which each tile compares against ("$40 in the previous 30 days"). 2 × 90 =
 * 180 days, under the route's MAX_SPAN_DAYS of 366. `first_day` comes back as
 * the first row in the DOUBLED range, which is what makes it usable as a
 * previous-period coverage test — see contract 3 for what it does not mean.
 */
export function rollupWindow(span: RollupSpan, now: Date = new Date()) {
  const end = now.toISOString().slice(0, 10);
  return { start: dayOffset(end, -(2 * span - 1)), windowStart: dayOffset(end, -(span - 1)), end };
}

export function useCompanyRollup(span: RollupSpan = 30) {
  const api = useApi();
  const { start, windowStart, end } = rollupWindow(span);
  const { data, error, isLoading, mutate } = useSWR<CompanyRollup>(
    `/company/rollup?start=${start}&end=${end}`,
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
    /** First day of the CURRENT period; earlier rows are the previous one. */
    windowStart,
    span,
    error,
    isLoading,
    refresh: mutate,
    /** A column the backend has told us it cannot serve. */
    isUnavailable: (key: string) => !!data?.unavailable?.includes(key),
  };
}
