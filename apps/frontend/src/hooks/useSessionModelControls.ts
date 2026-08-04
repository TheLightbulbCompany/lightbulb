"use client";

import { useUser } from "@clerk/nextjs";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { buildChatSessionKey } from "@isol8/shared";
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

  // Plain functions: the project's React Compiler auto-memoizes hook returns, and
  // a manual useCallback whose body can throw (keyOf) can't be preserved by the
  // compiler (react-compiler/react-compiler "memoization could not be preserved").
  const keyOf = (): string => {
    if (!agentId || !user?.id) throw new Error("No active agent or user");
    return buildChatSessionKey(agentId, user.id, activeSessionId);
  };

  const setModel = async (entry: ModelEntry): Promise<void> => {
    // Idle-only: a mid-run switch throws server-side (LiveSessionModelSwitchError).
    if (isStreaming) throw new LiveSessionModelSwitchError();
    await callRpc("sessions.patch", {
      key: keyOf(),
      model: buildModelRef(entry.provider, entry.id),
    });
  };

  const setThinkingLevel = async (level: string): Promise<void> => {
    await callRpc("sessions.patch", { key: keyOf(), thinkingLevel: level });
  };

  const setFastMode = async (fast: boolean): Promise<void> => {
    await callRpc("sessions.patch", { key: keyOf(), fastMode: fast });
  };

  return { setModel, setThinkingLevel, setFastMode };
}
