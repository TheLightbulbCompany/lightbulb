"use client";

import { useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { useApi } from "@/lib/api";

export type CreditsBalance = {
  /** Metronome prepaid balance in microcents, or null when unavailable. */
  balance_microcents: number | null;
  balance_dollars: string | null;
  /** Server-computed bedrock-gate predicate. */
  is_exhausted?: boolean;
};

export function isCreditsExhausted(balance: CreditsBalance | null | undefined): boolean {
  if (!balance) return false;
  return (
    balance.is_exhausted ??
    (balance.balance_microcents !== null && balance.balance_microcents <= 0)
  );
}

export function isCreditsBlocked(
  balance: CreditsBalance | null | undefined,
  providerChoice: string | null | undefined,
): boolean {
  return providerChoice === "bedrock_claude" && isCreditsExhausted(balance);
}

export type TopUpResult = {
  /** Metronome collects the saved Stripe payment method asynchronously. */
  status: string;
  amount_cents: number;
};

/** Workspace-scoped Metronome balance and one explicit payment-gated top-up. */
export function useCredits() {
  const api = useApi();
  const { orgId } = useAuth();
  const requestIdRef = useRef<string | null>(null);
  if (requestIdRef.current === null) requestIdRef.current = crypto.randomUUID();

  const { data, error, isLoading, mutate } = useSWR<CreditsBalance>(
    orgId ? ["/billing/credits/balance", orgId] : null,
    ([path]: [string, string]) => api.get(path) as Promise<CreditsBalance>,
    { refreshInterval: 30_000, dedupingInterval: 30_000 },
  );

  const startTopUp = async (amountCents: number): Promise<TopUpResult> =>
    (await api.post("/billing/credits/top_up", {
      amount_cents: amountCents,
      request_id: requestIdRef.current,
    })) as TopUpResult;

  return { balance: data, isLoading, error, refresh: mutate, startTopUp };
}
