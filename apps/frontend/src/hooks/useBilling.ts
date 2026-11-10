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
  // Server allowlist: ChatGPT sign-in is offered only to internal workspaces.
  chatgpt_provider_available?: boolean;
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
  // The one plan: workspace_price_usd covers included_members people, then
  // seat_price_usd a month for each member past them. Optional for old fixtures.
  seat_price_usd?: number;
  included_members?: number;
  // The Front desk add-on: whether this workspace has it, and its monthly price. Optional for old fixtures.
  front_desk?: boolean;
  front_desk_price_usd?: number | null;
  managed_markup?: string | null;
  // The managed model's per-key daily limit tripped today (UTC); clears at the rollover.
  capped_today?: boolean;
  // Epoch seconds the workspace's own provider plan (ChatGPT, BYO key) is back from a usage
  // limit; null once passed. Display only.
  provider_quota_until?: number | null;
  // Capped owners only: OpenRouter's own daily usage on the owner's key. Absent on a read failure.
  allowance?: { used_usd: number; limit_usd: number; resets_at: number } | null;
  // Why this owner's agents cannot run right now (backend chat_gate.budget_block_reason);
  // drives the messaging shell's limit bar. Null/absent when they can.
  block_reason?: "trial_expired" | "trial_daily_cap" | "included_daily_cap" | "out_of_credits" | null;
  // Which channel took the money — "apple_iap", or absent/null for Stripe (every
  // pre-IAP row). Stripe has no record of an Apple subscription, so the portal
  // and checkout must not be offered for one (see routers/billing.py's
  // "apple_managed" 409 and apps/mobile/src/lib/manageSubscription.ts).
  subscription_source?: "apple_iap" | "stripe" | null;
}

export interface MemberUsage {
  user_id: string;
  display_name: string | null;
  email: string | null;
  total_spend: number;
  total_input_tokens: number;
  total_output_tokens: number;
  request_count: number;
  capture_prompt_tokens?: number;
  capture_completion_tokens?: number;
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

  const changeProvider = useCallback(async (provider_choice: "chatgpt_oauth" | "openrouter_managed") => {
    const result = (await api.post("/billing/provider", { provider_choice })) as {
      status: "ok" | "noop";
      provider_choice: "chatgpt_oauth" | "openrouter_managed";
    };
    await mutate();
    return result;
  }, [api, mutate]);

  // `confirm` is required to change a live subscription's plan; without it the
  // server changes nothing and answers `confirm_required`.
  // `then`: Checkout returns straight into ChatGPT sign-in (Settings, or the onboarding step) for a free workspace.
  const startCheckout = useCallback(async (plan: "workspace", confirm = false, then?: "chatgpt" | "chatgpt_onboarding" | "managed_onboarding") => {
    const res = (await api.post("/billing/checkout", { plan, confirm, ...(then ? { then } : {}) })) as {
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
    changeProvider,
    fetchUsage,
    refresh,
  };
}
