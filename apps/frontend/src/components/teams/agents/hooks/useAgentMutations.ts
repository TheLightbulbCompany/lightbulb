// apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import type { CompanyAgent } from "@/components/teams/shared/types";

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
      // The gateway acks agents.delete BEFORE the backend cascades the
      // Paperclip row removal: websocket_chat sends the WS success, THEN runs
      // EFS cleanup + delete_paperclip_for_chat. A blind revalidate here races
      // that cascade and can refetch the still-present row, resurrecting the
      // agent in the UI until a manual refresh. Drop it from the caches
      // optimistically WITHOUT refetching — the agent is gone; any natural
      // later revalidation reflects the server once the cascade has run.
      mutate(
        AGENTS_KEY,
        (cur: { agents?: CompanyAgent[] } | undefined) =>
          cur?.agents ? { ...cur, agents: cur.agents.filter((a) => a.id !== agentId) } : cur,
        { revalidate: false },
      );
      mutate(`${AGENTS_KEY}/${agentId}`, undefined, { revalidate: false });
    },
    [callRpc, mutate],
  );

  return { setReportsTo, pause, resume, remove };
}
