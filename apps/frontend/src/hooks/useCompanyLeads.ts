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
  if (error instanceof ApiError) {
    if (error.status === 403) return { kind: "forbidden" };
    const code = (error.body as { detail?: { code?: string } } | null)?.detail?.code;
    if (code === "apollo_key_rejected") return { kind: "key_rejected" };
  }
  if (error) return { kind: "unavailable" };
  if (accumulated) return { kind: "rows" };
  if (isLoading || !data) return { kind: "loading" };
  if (!data.connected) return { kind: "unbound" };
  return data.data.length ? { kind: "rows" } : { kind: "zero" };
}

export function useCompanyLeads() {
  const api = useApi();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [earlier, setEarlier] = useState<LeadRow[]>([]);

  const key = query ? `/company/leads?q=${encodeURIComponent(query)}&page=${page}` : null;
  const { data, error, isLoading } = useSWR<LeadsEnvelope>(
    key,
    (path: string) => api.get(path) as Promise<LeadsEnvelope>,
    { dedupingInterval: 60_000, keepPreviousData: true, revalidateOnFocus: false },
  );

  const search = useCallback((q: string) => {
    setEarlier([]);
    setPage(1);
    setQuery(q.trim());
  }, []);

  const next = useCallback(() => {
    if (!data?.has_more) return;
    setEarlier((prev) => [...prev, ...data.data]);
    setPage((p) => p + 1);
  }, [data]);

  const rows = [...earlier, ...(data?.data ?? [])];
  return {
    query,
    search,
    rows,
    total: data?.total ?? 0,
    view: leadsView(query, data, error, isLoading, earlier.length),
    hasMore: !!data?.has_more,
    next,
  };
}
