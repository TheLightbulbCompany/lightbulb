"use client";

import { useCallback } from "react";
import { useUser } from "@clerk/nextjs";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { buildChatSessionKey } from "@/lib/sessionKey";
import { buildModelRef, type ModelEntry } from "@/lib/models";

/**
 * Thrown when the user tries to switch the model while a run is live. OpenClaw
 * also rejects a mid-run model change (LiveSessionModelSwitchError), so we gate
 * client-side to keep the UI honest and skip a doomed RPC.
 */
export class LiveSessionModelSwitchError extends Error {
  constructor() {
    super("Stop the current run before switching model.");
    this.name = "LiveSessionModelSwitchError";
  }
}

export interface SessionModelControls {
  setModel: (entry: ModelEntry) => Promise<void>;
  setThinkingLevel: (level: string) => Promise<void>;
  setFastMode: (fast: boolean) => Promise<void>;
}

/**
 * Persist composer selections onto the active session via sessions.patch — the
 * EXISTING gateway operator path (OpenClaw rejects session mutations from
 * webchat connections; the backend connects as operator). No new route.
 */
export function useSessionModelControls(
  agentId: string | null,
  activeSessionId: string | null,
  isStreaming: boolean,
): SessionModelControls {
  const { user } = useUser();
  const callRpc = useGatewayRpcMutation();

  const keyOf = useCallback((): string => {
    if (!agentId || !user?.id) throw new Error("No active agent or user");
    return buildChatSessionKey(agentId, user.id, activeSessionId);
  }, [agentId, user?.id, activeSessionId]);

  const setModel = useCallback(
    async (entry: ModelEntry): Promise<void> => {
      // Idle-only: a mid-run switch throws server-side (LiveSessionModelSwitchError).
      if (isStreaming) throw new LiveSessionModelSwitchError();
      await callRpc("sessions.patch", {
        key: keyOf(),
        model: buildModelRef(entry.provider, entry.id),
      });
    },
    [callRpc, keyOf, isStreaming],
  );

  const setThinkingLevel = useCallback(
    async (level: string): Promise<void> => {
      await callRpc("sessions.patch", { key: keyOf(), thinkingLevel: level });
    },
    [callRpc, keyOf],
  );

  const setFastMode = useCallback(
    async (fast: boolean): Promise<void> => {
      await callRpc("sessions.patch", { key: keyOf(), fastMode: fast });
    },
    [callRpc, keyOf],
  );

  return { setModel, setThinkingLevel, setFastMode };
}
