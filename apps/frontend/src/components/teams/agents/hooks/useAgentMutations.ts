// apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

// All /teams reads use keys prefixed "/teams"; the agents list key is
// "/teams/agents". Invalidate it after every mutation so the org chart and
// agents list re-render with fresh data (status dots also update via events).
const AGENTS_KEY = "/teams/agents";

export function useAgentMutations() {
  const { patch, post } = useTeamsApi();
  const callRpc = useGatewayRpcMutation();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(() => {
    mutate(AGENTS_KEY);
  }, [mutate]);

  const setReportsTo = useCallback(
    async (agentId: string, managerId: string | null) => {
      // Empty string clears the manager (root); BFF forwards it to Paperclip.
      await patch(`/agents/${agentId}`, { reportsTo: managerId ?? "" });
      revalidate();
    },
    [patch, revalidate],
  );

  const pause = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/pause`, {});
      revalidate();
    },
    [post, revalidate],
  );

  const resume = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/resume`, {});
      revalidate();
    },
    [post, revalidate],
  );

  // Delete goes through the OpenClaw gateway (removes the container entry +
  // cascades to Paperclip via the websocket_chat interceptor). The Teams BFF
  // DELETE would only remove the Paperclip row, which a reprovision resurrects.
  const remove = useCallback(
    async (agentId: string) => {
      await callRpc("agents.delete", { agentId, deleteFiles: true });
      revalidate();
    },
    [callRpc, revalidate],
  );

  return { setReportsTo, pause, resume, remove };
}
