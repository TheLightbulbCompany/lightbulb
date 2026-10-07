"use client";

import { useCallback } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { useApi, ApiError } from "@/lib/api";

// =============================================================================
// Flat-fee billing hook. Backend response shapes match
// apps/backend/schemas/billing.py.
// =============================================================================

export interface BillingAccount {
  // PAID predicate (backend `is_paid_subscription`): true for active /
  // legacy-trialing rows, FALSE for the no-card trial_local free tier —
  // free-tier users must see a Subscribe CTA, never "Active subscription".
  // Platform-use gating (which trial_local DOES pass) is server-side.
  is_subscribed: boolean;
  current_spend: number;
  lifetime_spend: number;
  // Stripe-native subscription state. Both null until the user signs up.
  subscription_status: string | null;
  trial_end: number | null;
  // The owner-keyed billing row carries the chosen inference path. Used by
  // ProvisioningStepper to skip the ProviderPicker for org members joining
  // an org whose admin has already picked.
  provider_choice: string | null;
  byo_provider: string | null;
  managed_inference_available?: boolean;
  // Pricing tier: "standard" (the single $50/mo tier) or "ultra_pro" (the
  // higher $150/mo tier that unlocks larger marketplace companies). Backend
  // defaults a plan-less PAID row to "standard" and reports null for a trial
  // or no row; optional here so test fixtures don't have to carry it.
  plan?: "base" | "standard" | "ultra_pro" | null;
  // Workspace prices are server-owned so every client shows the deployed
  // catalog instead of a stale price baked into a release.
  workspace_price_usd: number;
  base_price_usd: number;
  ultra_price_usd: number;
}

export interface MemberUsage {
  user_id: string;
  display_name: string | null;
  email: string | null;
  total_spend: number;
  total_input_tokens: number;
  total_output_tokens: number;
  request_count: number;
}

export interface UsageSummary {
  period: string;
  total_spend: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  request_count: number;
  lifetime_spend: number;
  by_member: MemberUsage[];
}

interface PortalResponse {
  portal_url: string;
}

export function useBilling() {
  const { isSignedIn } = useAuth();
  const api = useApi();

  // /billing/account returns 404 when the owner has no billing row yet
  // (pre-subscribe, pre-Stripe-customer). Treat that as the empty state
  // ("no account") rather than an error so the UI can render the
  // subscribe CTA. Any other ApiError surfaces normally.
  const fetcher = useCallback(
    async (url: string): Promise<BillingAccount | null> => {
      try {
        return (await api.get(url)) as BillingAccount;
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
    [api],
  );

  const {
    data: account,
    error,
    isLoading,
    mutate,
  } = useSWR<BillingAccount | null>(
    isSignedIn ? "/billing/account" : null,
    fetcher,
    {
      revalidateOnFocus: false,
      dedupingInterval: 30000,
    },
  );

  const openPortal = useCallback(async () => {
    const { portal_url } = (await api.post("/billing/portal", {})) as PortalResponse;
    window.location.href = portal_url;
  }, [api]);

  const startCheckout = useCallback(async (plan: "base" | "workspace" | "ultra") => {
    const res = (await api.post("/billing/checkout", { plan })) as {
      checkout_url?: string;
      updated?: boolean;
    };
    if (res.checkout_url) {
      window.location.href = res.checkout_url;
    } else {
      await mutate();
    }
  }, [api, mutate]);

  // Same 404→null contract as /billing/account.
  const fetchUsage = useCallback(async (): Promise<UsageSummary | null> => {
    try {
      return (await api.get("/billing/usage")) as UsageSummary;
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  }, [api]);

  const isSubscribed = account?.is_subscribed === true;
  const refresh = useCallback(() => mutate(), [mutate]);

  return {
    account,
    isLoading,
    error,
    isSubscribed,
    openPortal,
    startCheckout,
    fetchUsage,
    refresh,
  };
}
