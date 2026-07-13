"use client";

import useSWR from "swr";
import { useApi } from "@/lib/api";

export interface SpendByModel {
  model: string;
  tier: string;
  cents: number;
}

export interface SpendQuotaWindow {
  label: string;
  used_percent: number;
  resets_at_ms: number | null;
}

export interface SpendResponse {
  mode: "bedrock" | "oauth_quota" | "tokens_only";
  billed_month_to_date_cents: number | null;
  by_model: SpendByModel[] | null;
  period_start: string | null;
  plan: string | null;
  windows: SpendQuotaWindow[] | null;
}

/** Provider-aware spend summary. tokens_only means: render the token grid
 * with no dollar/quota block (BYOK, trial, or degraded upstream). */
export function useSpend() {
  const api = useApi();
  const { data, isLoading } = useSWR<SpendResponse>(
    "/billing/spend",
    () => api.get("/billing/spend") as Promise<SpendResponse>,
    { dedupingInterval: 30000 },
  );
  return { spend: data, isLoading };
}
