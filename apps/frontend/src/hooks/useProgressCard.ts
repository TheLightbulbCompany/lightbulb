// frontend/src/hooks/useProgressCard.ts
/**
 * OpenClaw 8.1 durable progress cards.
 *
 * Agents on 8.1 persist a progress card (steps + optional markdown notes)
 * per session via the `progress_card` tool (per-agent SQLite, survives
 * reload) — unlike the live `agent_activity` pill in useAgentChat, which
 * lives only in in-memory refs and disappears on refresh.
 *
 * `progressCard.get {sessionKey}` reads it; `progressCard.changed
 * {sessionKey, revision}` is forwarded verbatim from the gateway whenever it
 * changes. `progressCard.put` is agent-side only — the frontend never calls
 * it.
 */
"use client";

import { useEffect } from "react";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";

export type ProgressCardStepStatus = "pending" | "in_progress" | "completed";

export interface ProgressCardStep {
  step: string;
  status: ProgressCardStepStatus;
}

export interface ProgressCard {
  sessionKey: string;
  revision: number;
  updatedAt: number;
  markdown?: string;
  steps?: ProgressCardStep[];
}

interface ProgressCardGetResult {
  card: ProgressCard | null;
}

const PROGRESS_CARD_GET_METHOD = "progressCard.get";
const PROGRESS_CARD_CHANGED_EVENT = "progressCard.changed";

export interface UseProgressCardResult {
  card: ProgressCard | null;
  isLoading: boolean;
  error: Error | undefined;
}

/** Read the durable progress card for `sessionKey` (null while there's no
 *  active session), kept fresh by the gateway's push event. */
export function useProgressCard(sessionKey: string | null): UseProgressCardResult {
  const { onEvent } = useGateway();
  const { data, error, isLoading, mutate } = useGatewayRpc<ProgressCardGetResult>(
    sessionKey ? PROGRESS_CARD_GET_METHOD : null,
    sessionKey ? { sessionKey } : undefined,
  );

  // useGatewayRpc's own event-name matching (method === event ||
  // method.startsWith(event + ".")) doesn't fire here: the RPC method is
  // "progressCard.get" and the push event is "progressCard.changed" — same
  // namespace, different leaf. Revalidate by hand, scoped to this session.
  useEffect(() => {
    if (!sessionKey) return;
    return onEvent((event, payload) => {
      if (event !== PROGRESS_CARD_CHANGED_EVENT) return;
      const eventSessionKey =
        payload && typeof payload === "object"
          ? (payload as Record<string, unknown>).sessionKey
          : undefined;
      if (eventSessionKey !== sessionKey) return;
      void mutate();
    });
  }, [sessionKey, onEvent, mutate]);

  return { card: data?.card ?? null, isLoading, error };
}
