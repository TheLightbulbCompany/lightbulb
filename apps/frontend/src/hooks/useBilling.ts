"use client";

import { useCallback } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { useApi, ApiError } from "@/lib/api";
import type { ProviderChoiceId } from "@/lib/providerCards";

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
  // Pricing tier: "standard" (the single $50/mo tier) or "ultra_pro" (the
  // higher $150/mo tier that unlocks larger marketplace companies). Backend
  // defaults a plan-less row to "standard"; optional here so pre-plan
  // response shapes (and test fixtures) don't have to carry it.
  plan?: "standard" | "ultra_pro";
  // Teams per-seat billing (org owners only; null for personal). seat_count is
  // the live org member count; team_seat_price_usd is echoed from backend
  // config so the paywall never hardcodes the per-seat price.
  seat_count: number | null;
  team_seat_price_usd: number | null;
  // Flat monthly base charged alongside the per-seat item. null for personal
  // owners.
  team_base_price_usd?: number | null;
  // True only for org owners on a REAL per-seat Stripe subscription (one
  // that recorded a subscription_item_id via the webhook). False for
  // personal owners and for flat/comped/grandfathered org subs — those
  // must not render seat-priced "N x $price" billing math.
  is_seat_subscription: boolean;
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

  const startTeamCheckout = useCallback(async () => {
    const { checkout_url } = (await api.post("/billing/team-checkout", {})) as {
      checkout_url?: string;
    };
    if (!checkout_url) throw new Error("No checkout_url returned");
    window.location.href = checkout_url;
  }, [api]);

  // Flat-fee subscribe for a free-tier (trial_local) owner. POST
  // /billing/trial-checkout is the flat $50/mo checkout despite the legacy
  // path name (billed immediately, no Stripe trial) — the same route the
  // ProviderPicker cards hit at signup.
  //
  // The provider is a REQUIRED argument. It used to be hardcoded to
  // `bedrock_claude` on the reasoning that the free tier already runs
  // Bedrock-managed Claude, so conversion should keep that path. Two things
  // were wrong with that. The owner never got to choose — the other two
  // provider paths cost us nothing in inference, and nobody was ever offered
  // them at the one moment they are deciding to pay. And Checkout's
  // success_url carries `?provider=<id>` into the wizard's provider step, so
  // a hardcoded choice made server-side rather than picked client-side ALSO
  // skipped CreditsStep — leaving converted owners on Bedrock-managed
  // inference with a zero credit balance they were never asked to fund.
  // `creditsCents` is the starter Claude-credit amount bundled into the same
  // Checkout as a one-time line item — bedrock_claude only, since the other
  // two providers spend no Isol8 credits. Omitted => the server default. The
  // server re-validates and re-scopes it; this is a UI convenience, never the
  // authority on what gets charged.
  const startTrialCheckout = useCallback(async (
    providerChoice: ProviderChoiceId,
    creditsCents?: number,
  ) => {
    const { checkout_url } = (await api.post("/billing/trial-checkout", {
      provider_choice: providerChoice,
      ...(creditsCents != null ? { credits_cents: creditsCents } : {}),
    })) as { checkout_url?: string };
    if (!checkout_url) throw new Error("No checkout_url returned");
    window.location.href = checkout_url;
  }, [api]);

  // Upgrade the owner to the Ultra Pro tier via POST /billing/ultra-checkout.
  // Two response shapes (see routers/billing.py create_ultra_checkout):
  //   - {checkout_url}   → free-tier / no live sub: a fresh Ultra Checkout
  //                        session; redirect the browser to Stripe.
  //   - {upgraded: true} → owner already on the flat sub: an in-place item
  //                        swap (one subscription, prorated), no redirect —
  //                        refresh the billing row so the card flips to the
  //                        Ultra badge once the webhook records the tier.
  const upgradeToUltra = useCallback(async () => {
    const res = (await api.post("/billing/ultra-checkout", {})) as {
      checkout_url?: string;
      upgraded?: boolean;
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
    startTeamCheckout,
    startTrialCheckout,
    upgradeToUltra,
    fetchUsage,
    refresh,
  };
}
