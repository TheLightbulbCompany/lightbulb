"use client";

import { useCallback, useRef } from "react";
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
  /** Server-computed: is this balance unspendable at the bedrock-gate? The
   *  gate admits only while remaining exceeds one reservation hold, so a small
   *  positive balance still buys nothing. Computed backend-side so the reserve
   *  constant has no TypeScript copy to drift from. Absent (not-yet-redeployed
   *  backend) is treated as not exhausted. */
  is_exhausted?: boolean;
};

/**
 * Is this balance unspendable at the bedrock-gate? The single predicate
 * shared by every surface that needs to gate on it (OutOfCreditsBanner,
 * CreditsStep) so they can never drift apart on when to show the "out of
 * credits" banner vs. when to skip the top-up step.
 *
 * `is_exhausted` is the source of truth once the backend carries the field —
 * computed server-side against the gate's own admission threshold (remaining
 * - reserved - estimate > 0), not `> 0`, since a small positive balance can
 * still be refused at the gate. During the frontend-ahead-of-backend deploy
 * window (Vercel auto-deploys on push; the backend's ECR/ECS rolling deploy
 * lands minutes later), the field is absent — fall back to the legacy
 * `balance_microcents <= 0` check so a genuinely zero-balance owner isn't
 * left with no banner (or skipped past the top-up step) for that window. A
 * null `balance_microcents` (Metronome unavailable / owner not provisioned
 * there yet) is indeterminate under BOTH predicates and must never read as
 * exhausted — never scare a user with a false banner, and never block a
 * user who might already be funded from proceeding.
 */
export function isCreditsExhausted(balance: CreditsBalance | null | undefined): boolean {
  if (!balance) return false;
  return (
    balance.is_exhausted ??
    (balance.balance_microcents !== null && balance.balance_microcents <= 0)
  );
}

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
      // card), so revalidate on an interval to surface the new balance without
      // forcing the user to refresh the page manually.
      refreshInterval: 30_000,
      // Matched to the interval because this hook has several always-mounted
      // consumers (OutOfCreditsBanner, CreditsSection, CreditsStep) and SWR's
      // polling timer is per-hook-instance. Their timers drift apart by more
      // than the 2s default dedupe window, so each mount was contributing its
      // own round trip; a matched dedupe collapses them back to one.
      dedupingInterval: 30_000,
    },
  );

  // One idempotency token per form session (hook mount), DELIBERATELY stable
  // across retries: if the response is lost in transit, the user's retry
  // carries the same request_id, so the backend's Metronome Idempotency-Key
  // replays the original commit instead of charging the card twice. No
  // regeneration after success is needed — the consuming surfaces latch
  // their submit button once a top-up is accepted. The ONE exception is
  // rotateRequestId() below, for a caller that has concluded the prior attempt
  // is dead and wants a genuinely new intent.
  const requestIdRef = useRef<string | null>(null);
  if (requestIdRef.current === null) {
    requestIdRef.current = crypto.randomUUID();
  }

  // Rotate to a FRESH idempotency token, starting a new top-up intent. The
  // token is otherwise stable across retries (see above) so a lost-response
  // retry replays rather than double-charges — but a caller that has concluded
  // the prior attempt is DEAD (e.g. CreditsStep's collect-timeout backstop:
  // the charge was accepted but never collected, so no credits will ever land)
  // MUST rotate first. Without it the "retry" reuses the dead token and the
  // backend replays the same commit — or 409s on an idempotency/body conflict
  // if the amount changed — re-sticking the user in the same failed cycle.
  const rotateRequestId = useCallback(() => {
    requestIdRef.current = crypto.randomUUID();
  }, []);

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
    rotateRequestId,
    startTopUpCheckout,
    disableAutoCharge,
  };
}
