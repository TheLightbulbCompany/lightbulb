// apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

// All /teams reads use keys prefixed "/teams". The agents LIST key is
// "/teams/agents" (org chart + agents list); the per-agent DETAIL key is
// "/teams/agents/{id}" (AgentDetailPanel/AgentManageSection's status +
// reportsTo). Every mutation invalidates BOTH so the detail view doesn't show
// stale state after a change (status dots also update via events).
const AGENTS_KEY = "/teams/agents";

export function useAgentMutations() {
  const { patch, post } = useTeamsApi();
  const callRpc = useGatewayRpcMutation();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(
    (agentId: string) => {
      mutate(AGENTS_KEY);
      mutate(`${AGENTS_KEY}/${agentId}`);
    },
    [mutate],
  );

  const setReportsTo = useCallback(
    async (agentId: string, managerId: string | null) => {
      // Empty string clears the manager (root); BFF forwards it to Paperclip.
      await patch(`/agents/${agentId}`, { reportsTo: managerId ?? "" });
      revalidate(agentId);
    },
    [patch, revalidate],
  );

  const pause = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/pause`, {});
      revalidate(agentId);
    },
    [post, revalidate],
  );

  const resume = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/resume`, {});
      revalidate(agentId);
    },
    [post, revalidate],
  );

  // Delete goes through the OpenClaw gateway (removes the container entry +
  // cascades to Paperclip via the websocket_chat interceptor). The Teams BFF
  // DELETE would only remove the Paperclip row, which a reprovision resurrects.
  const remove = useCallback(
    async (agentId: string) => {
      await callRpc("agents.delete", { agentId, deleteFiles: true });
      revalidate(agentId);
    },
    [callRpc, revalidate],
  );

  return { setReportsTo, pause, resume, remove };
}
