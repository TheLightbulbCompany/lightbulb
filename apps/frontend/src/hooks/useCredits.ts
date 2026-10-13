"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";

import { notifyApiError } from "@/lib/notify";
import { useApi } from "@/lib/api";

export type CreditsBalance = {
  /** Metronome prepaid balance in microcents, or null when unavailable. */
  balance_microcents: number | null;
  balance_dollars: string | null;
  /** Server-computed managed-credit gate predicate. */
  is_exhausted?: boolean;
  active_top_up?: {
    request_id: string;
    amount_cents: number;
    status: "pending" | "requested" | "action_required" | "paid" | "failed";
  } | null;
};

export function isCreditsExhausted(balance: CreditsBalance | null | undefined): boolean {
  if (!balance) return false;
  return (
    balance.is_exhausted ??
    (balance.balance_microcents !== null && balance.balance_microcents <= 0)
  );
}

/**
 * Below $5 the balance reads as "running low". Same line as the backend's
 * Metronome warn alert (`METRONOME_LOW_BALANCE_THRESHOLD_CENTS = 500`); the
 * balance unit is 1e-6 dollars (`balance_dollars = balance_microcents / 1e6`).
 */
export const LOW_CREDITS_MICROCENTS = 5_000_000;

export function isCreditsLow(balance: CreditsBalance | null | undefined): boolean {
  return (
    !!balance &&
    balance.balance_microcents !== null &&
    balance.balance_microcents < LOW_CREDITS_MICROCENTS &&
    !isCreditsExhausted(balance)
  );
}

export function isCreditsBlocked(
  balance: CreditsBalance | null | undefined,
  providerChoice: string | null | undefined,
): boolean {
  return providerChoice === "openrouter_managed" && isCreditsExhausted(balance);
}

export type TopUpResult = {
  /** Metronome collects the saved Stripe payment method asynchronously. */
  status: string;
  amount_cents: number;
};

type PortalResponse = { portal_url: string };

export type PendingTopUp = {
  user_id: string;
  workspace_id: string;
  request_id: string;
  amount_cents: number;
  state: "pending" | "requested" | "action_required";
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
      typeof intent.amount_cents !== "number" ||
      (intent.state !== "pending" && intent.state !== "requested" && intent.state !== "action_required")
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
  const authKey = ready && userId && orgId ? pendingTopUpKey(userId, orgId) : null;
  const currentAuthKey = useRef(authKey);
  useLayoutEffect(() => { currentAuthKey.current = authKey; }, [authKey]);
  const [outcome, setTopUpOutcome] = useState<{ authKey: string; status: "paid" | "failed" } | null>(null);
  const topUpOutcome = outcome?.authKey === authKey ? outcome.status : null;
  const pendingTopUp = ready && userId && orgId ? readPendingTopUp(userId, orgId) : null;
  const balancePath = pendingTopUp
    ? `/billing/credits/balance?request_id=${encodeURIComponent(pendingTopUp.request_id)}`
    : "/billing/credits/balance";

  const { data, error, isLoading, mutate } = useSWR<CreditsBalance>(
    ready && userId && orgId ? [balancePath, userId, orgId] : null,
    async ([path, fetchUserId, fetchOrgId]: [string, string, string]) => {
      const fetchAuthKey = pendingTopUpKey(fetchUserId, fetchOrgId);
      const balance = await api.get(path) as CreditsBalance;
      if (currentAuthKey.current !== fetchAuthKey) return balance;
      const persisted = readPendingTopUp(fetchUserId, fetchOrgId);
      const active = balance.active_top_up;
      if (persisted && active && active.request_id === persisted.request_id) {
        if (active.status === "paid" || active.status === "failed") {
          window.localStorage.removeItem(fetchAuthKey);
          setTopUpOutcome({ authKey: fetchAuthKey, status: active.status });
          refreshPendingTopUp((version) => version + 1);
        } else if (active.status !== persisted.state) {
          window.localStorage.setItem(
            fetchAuthKey,
            JSON.stringify({ ...persisted, state: active.status }),
          );
          refreshPendingTopUp((version) => version + 1);
        }
      }
      return balance;
    },
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
      state: "pending" as const,
    };
    if (!persisted) {
      setTopUpOutcome(null);
      window.localStorage.setItem(pendingTopUpKey(userId, orgId), JSON.stringify(intent));
      refreshPendingTopUp((version) => version + 1);
    }

    const result = await api.post("/billing/credits/top_up", {
      amount_cents: intent.amount_cents,
      request_id: intent.request_id,
    }) as TopUpResult;
    if (result.status === "requested") {
      // A request acknowledgement means Metronome accepted the gated commit;
      // collection is still asynchronous. Keep the exact locked intent across
      // reloads so the next UI cannot authorize a second purchase.
      window.localStorage.setItem(
        pendingTopUpKey(userId, orgId),
        JSON.stringify({ ...intent, state: "requested" }),
      );
      refreshPendingTopUp((version) => version + 1);
    }
    return result;
  };

  const openBilling = async () => {
    try {
      const { portal_url } = await api.post("/billing/portal", {}) as PortalResponse;
      window.location.href = portal_url;
    } catch (err) {
      notifyApiError(err, "Couldn't open billing");
    }
  };

  return {
    scopeKey: authKey,
    balance: data,
    isLoading,
    error,
    refresh: mutate,
    startTopUp,
    openBilling,
    pendingTopUp,
    topUpOutcome,
    canManageCredits: ready && orgRole === "org:admin",
  };
}
