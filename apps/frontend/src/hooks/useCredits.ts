"use client";

import { useState } from "react";
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

export type PendingTopUp = {
  user_id: string;
  workspace_id: string;
  request_id: string;
  amount_cents: number;
};

const pendingTopUpKey = (userId: string, workspaceId: string) =>
  `lightbulb:credits-top-up:${userId}:${workspaceId}`;

function readPendingTopUp(userId: string, workspaceId: string): PendingTopUp | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(pendingTopUpKey(userId, workspaceId));
    if (!raw) return null;
    const intent = JSON.parse(raw) as Partial<PendingTopUp>;
    if (
      intent.user_id !== userId ||
      intent.workspace_id !== workspaceId ||
      typeof intent.request_id !== "string" ||
      typeof intent.amount_cents !== "number"
    ) {
      window.localStorage.removeItem(pendingTopUpKey(userId, workspaceId));
      return null;
    }
    return intent as PendingTopUp;
  } catch {
    return null;
  }
}

/** Workspace-scoped Metronome balance and one explicit payment-gated top-up. */
export function useCredits() {
  const api = useApi();
  const { orgId, userId, isLoaded, isSignedIn, orgRole } = useAuth();
  const ready = Boolean(isLoaded && isSignedIn && userId && orgId);
  const [, refreshPendingTopUp] = useState(0);
  const pendingTopUp = ready && userId && orgId ? readPendingTopUp(userId, orgId) : null;

  const { data, error, isLoading, mutate } = useSWR<CreditsBalance>(
    ready && userId && orgId ? ["/billing/credits/balance", userId, orgId] : null,
    ([path]: [string, string, string]) => api.get(path) as Promise<CreditsBalance>,
    { refreshInterval: 30_000, dedupingInterval: 30_000 },
  );

  const startTopUp = async (amountCents: number): Promise<TopUpResult> => {
    if (!ready || !userId || !orgId) throw new Error("Workspace authentication is not ready");
    const persisted = pendingTopUp ?? readPendingTopUp(userId, orgId);
    const intent = persisted ?? {
      user_id: userId,
      workspace_id: orgId,
      request_id: crypto.randomUUID(),
      amount_cents: amountCents,
    };
    if (!persisted) {
      window.localStorage.setItem(pendingTopUpKey(userId, orgId), JSON.stringify(intent));
      refreshPendingTopUp((version) => version + 1);
    }

    const result = await api.post("/billing/credits/top_up", {
      amount_cents: intent.amount_cents,
      request_id: intent.request_id,
    }) as TopUpResult;
    if (result.status === "requested") {
      window.localStorage.removeItem(pendingTopUpKey(userId, orgId));
      refreshPendingTopUp((version) => version + 1);
    }
    return result;
  };

  return {
    balance: data,
    isLoading,
    error,
    refresh: mutate,
    startTopUp,
    pendingTopUp,
    canManageCredits: ready && orgRole === "org:admin",
  };
}
