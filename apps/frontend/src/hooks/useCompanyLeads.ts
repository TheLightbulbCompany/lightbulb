"use client";

import { useCallback, useState } from "react";
import useSWR from "swr";
import { ApiError } from "@lightbulb/shared";
import { useApi } from "@/lib/api";

/**
 * `GET /api/v1/company/leads?q=&page=` — the first backend read over a tier-2
 * (`backend_only`) credential. Same envelope discipline as the Money tabs so
 * one mental model serves both:
 *
 *   connected: false   → `unbound` (no Apollo dashboard-access key pasted)
 *   200, data: []      → `zero`
 *   403                → `forbidden` (non-admin org member — permanent)
 *   502                → `unavailable`, except `apollo_key_rejected`, which
 *                        is its own state: the fix is re-pasting the key,
 *                        not waiting, and the copy must say so
 *
 * The search is NOT auto-fired: an owner opening Leads sees the prompt and
 * chips, not a stale result. `search()` sets the query; SWR fetches. Pages
 * accumulate (same reasoning as Money: swapping the key blanked the table).
 */

export interface LeadRow {
  id: string | null;
  first_name: string | null;
  /** Privacy-masked by Apollo on the free search ("L."). Real on enrichment. */
  last_name: string | null;
  title: string | null;
  organization: string | null;
  has_email: boolean;
  city: string | null;
  country: string | null;
}

export interface LeadsEnvelope {
  connected: boolean;
  data: LeadRow[];
  total: number;
  page: number;
  has_more: boolean;
}

export type LeadsView =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "key_rejected" }
  | { kind: "unavailable" }
  | { kind: "unbound" }
  | { kind: "zero" }
  | { kind: "rows" };

export function leadsView(
  query: string,
  data: LeadsEnvelope | undefined,
  error: unknown,
  isLoading: boolean,
  accumulated: number,
): LeadsView {
  if (!query) return { kind: "idle" };
  // A 403 is permanent and cannot co-occur with rows (page 1 would have
  // refused). Everything else — a 502 on page 3, a key revoked mid-session —
  // must not take away a table the owner is already reading.
  if (error instanceof ApiError && error.status === 403) return { kind: "forbidden" };
  if (accumulated) return { kind: "rows" };
  if (error instanceof ApiError) {
    const code = (error.body as { detail?: { code?: string } } | null)?.detail?.code;
    if (code === "apollo_key_rejected") return { kind: "key_rejected" };
  }
  if (error) return { kind: "unavailable" };
  if (isLoading || !data) return { kind: "loading" };
  if (!data.connected) return { kind: "unbound" };
  return data.data.length ? { kind: "rows" } : { kind: "zero" };
}

export function useCompanyLeads() {
  const api = useApi();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [earlier, setEarlier] = useState<LeadRow[]>([]);
  // The last successful total: while page N loads (or after it fails) there
  // is no envelope, and "0 people" over 25 visible rows is a contradiction.
  const [keptTotal, setKeptTotal] = useState(0);

  const key = query ? `/company/leads?q=${encodeURIComponent(query)}&page=${page}` : null;
  // No keepPreviousData: `earlier` is what keeps the table on screen between
  // pages. Lagged data here rendered the current page twice while the next
  // one loaded, and after a failed page it was re-appended as if it were new.
  const { data, error, isLoading, mutate } = useSWR<LeadsEnvelope>(
    key,
    (path: string) => api.get(path) as Promise<LeadsEnvelope>,
    { dedupingInterval: 60_000, revalidateOnFocus: false },
  );

  const search = useCallback((q: string) => {
    setEarlier([]);
    setKeptTotal(0);
    setPage(1);
    setQuery(q.trim());
  }, []);

  const next = useCallback(() => {
    // The requested page has not arrived (in flight, or it failed): retry the
    // same key rather than advancing past a page nobody has seen.
    if (!data) {
      void mutate();
      return;
    }
    if (!data.has_more) return;
    setEarlier((prev) => [...prev, ...data.data]);
    setKeptTotal(data.total);
    setPage((p) => p + 1);
  }, [data, mutate]);

  const rows = [...earlier, ...(data?.data ?? [])];
  // A later page failed while a table is on screen: keep the rows, say so.
  const moreFailed = !!error && earlier.length > 0;
  return {
    query,
    search,
    rows,
    total: data?.total ?? keptTotal,
    view: leadsView(query, data, error, isLoading, earlier.length),
    // Mid-pagination with nothing back yet, the button stays so it can retry.
    hasMore: data ? data.has_more : earlier.length > 0,
    moreFailed,
    next,
  };
}
