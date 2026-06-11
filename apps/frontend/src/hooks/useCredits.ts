"use client";

import { useRef } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";

export type CreditsBalance = {
  /** Metronome prepaid balance in microcents — null when unavailable
   *  (Metronome outage, or the owner has no Metronome customer yet). */
  balance_microcents: number | null;
  balance_dollars: string | null;
  /** Whether the Metronome credits lifecycle is wired for this environment.
   *  When false, top-ups are guaranteed to 400 — surfaces must offer a
   *  different provider path instead of the top-up form. Optional so a
   *  not-yet-redeployed backend (no field) is treated as enabled. */
  lifecycle_enabled?: boolean;
  /** Whether the owner has consented to one-click top-ups (card on file,
   *  no checkout redirect). Granted only via a prior Checkout with the
   *  opt-in box ticked; false/absent routes purchases through Checkout. */
  auto_charge_enabled?: boolean;
};

export type TopUpResult = {
  /** "requested" — Metronome charges the saved card and releases the
   *  credits asynchronously; there is no checkout redirect. */
  status: string;
  amount_cents: number;
};

export type TopUpCheckoutResult = {
  /** Stripe-hosted payment page — navigate the browser here. The credits
   *  grant lands via webhook after the customer confirms payment. */
  checkout_url: string;
};

export function useCredits() {
  const api = useApi();

  const { data, error, isLoading, mutate } = useSWR<CreditsBalance>(
    "/billing/credits/balance",
    (path: string) => api.get(path) as Promise<CreditsBalance>,
    {
      // Top-ups release asynchronously (Metronome collects via the saved
      // card), so revalidate quickly to surface the new balance without
      // forcing the user to refresh the page manually.
      refreshInterval: 30_000,
      revalidateOnFocus: true,
    },
  );

  // One idempotency token per form session (hook mount), DELIBERATELY stable
  // across retries: if the response is lost in transit, the user's retry
  // carries the same request_id, so the backend's Metronome Idempotency-Key
  // replays the original commit instead of charging the card twice. No
  // regeneration after success is needed — the consuming surfaces latch
  // their submit button once a top-up is accepted.
  const requestIdRef = useRef<string | null>(null);
  if (requestIdRef.current === null) {
    requestIdRef.current = crypto.randomUUID();
  }

  const startTopUp = async (amountCents: number): Promise<TopUpResult> => {
    return (await api.post("/billing/credits/top_up", {
      amount_cents: amountCents,
      request_id: requestIdRef.current,
    })) as TopUpResult;
  };

  // The default purchase path: a Stripe-hosted Checkout where the customer
  // explicitly confirms the payment. enableAutoCharge opts in to one-click
  // top-ups for NEXT time (the checkout captures the off-session card
  // consent; the backend flips the flag only after the payment succeeds).
  const startTopUpCheckout = async (
    amountCents: number,
    enableAutoCharge: boolean,
  ): Promise<TopUpCheckoutResult> => {
    return (await api.post("/billing/credits/top_up_checkout", {
      amount_cents: amountCents,
      enable_auto_charge: enableAutoCharge,
    })) as TopUpCheckoutResult;
  };

  // One-way: re-enabling requires a fresh consenting Checkout, so there is
  // deliberately no enable counterpart.
  const disableAutoCharge = async (): Promise<void> => {
    await api.post("/billing/credits/auto_charge/disable", {});
    void mutate();
  };

  return {
    balance: data,
    // false ONLY when the backend explicitly says so — undefined (still
    // loading, or an older backend without the field) must not flash the
    // credits-unavailable state at users in lifecycle-enabled envs.
    lifecycleEnabled: data ? data.lifecycle_enabled !== false : undefined,
    // Strict true: undefined/absent (loading, older backend) must route to
    // the safe default — explicit Checkout — never to an implicit charge.
    autoChargeEnabled: data?.auto_charge_enabled === true,
    // SWR v2 exposes a native `isLoading` flag that correctly reports
    // "no data yet, currently fetching" without the stale-while-revalidate
    // false-negative the legacy `!data && !error` idiom produces.
    isLoading,
    error,
    refresh: mutate,
    startTopUp,
    startTopUpCheckout,
    disableAutoCharge,
  };
}
