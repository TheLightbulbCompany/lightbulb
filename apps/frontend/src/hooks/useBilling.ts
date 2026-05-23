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
    fetchUsage,
    refresh,
  };
}
