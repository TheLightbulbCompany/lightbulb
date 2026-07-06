"use client";

import { useCallback } from "react";
import {
  agentDisplayName,
  getAgentModelString,
  type Agent,
  type AgentModelRef,
  type AgentsListResponse,
} from "@isol8/shared";
import { capture } from "@/lib/analytics";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

// Agent types + pure helpers moved to @isol8/shared (Phase 1 extraction);
// re-exported so feature code keeps importing them from here. The useAgents
// hook itself stays web-side — its mutations couple to PostHog analytics.
export { agentDisplayName, getAgentModelString };
export type { Agent, AgentModelRef };

export function useAgents() {
  const { data, error, isLoading, mutate } =
    useGatewayRpc<AgentsListResponse>("agents.list");
  const callRpc = useGatewayRpcMutation();

  const agents = data?.agents ?? [];
  const defaultId = data?.defaultId;

  const createAgent = useCallback(
    // OpenClaw's agents.create requires a non-empty `workspace` string
    // (it does NOT inherit agents.defaults.workspace at create time).
    // When the caller omits `workspace`, we fill in the same path the
    // default would resolve to at runtime — .openclaw/workspaces/{id} —
    // so the agent lands on EFS without the caller knowing about path
    // conventions.
    async (params: { name: string; workspace?: string; emoji?: string }) => {
      const normalizedId = params.name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/-+/g, "-")
        .replace(/^-|-$/g, "");
      const workspace = params.workspace ?? `.openclaw/workspaces/${normalizedId}`;
      // agents.create runs a long *serial* chain server-side: a Paperclip
      // company create, two container RPCs (agents.create + agents.update),
      // and an EFS-locked operator-device pairing. That regularly runs past
      // the generic 30s RPC budget while still succeeding — which surfaced a
      // successful create as "RPC timeout: agents.create" and dropped the new
      // agent from the list until a manual refresh. Two consequences here:
      //   1. a 90s budget for this one RPC, so the normal create no longer
      //      false-times-out; and
      //   2. revalidate agents.list in `finally`, so even a create that does
      //      blow past 90s still surfaces the agent on the next poll instead
      //      of vanishing. The agent must not silently disappear — a missing
      //      agent invites a re-click, and create is deliberately
      //      non-idempotent, so a second click mints a SECOND agent.
      try {
        await callRpc("agents.create", { ...params, workspace }, 90_000);
        capture("agent_created", {
          agent_id: normalizedId,
          agent_name: params.name,
          workspace,
        });
      } finally {
        await mutate();
      }
    },
    [callRpc, mutate],
  );

  const deleteAgent = useCallback(
    async (agentId: string) => {
      await callRpc("agents.delete", { agentId, deleteFiles: true });
      capture("agent_deleted", { agent_id: agentId });
      mutate();
    },
    [callRpc, mutate],
  );

  const updateAgent = useCallback(
    async (agentId: string, updates: { model?: string; name?: string }) => {
      await callRpc("agents.update", { agentId, ...updates });
      if (updates.model) {
        capture("agent_model_changed", { agent_id: agentId, model: updates.model });
      }
      mutate();
    },
    [callRpc, mutate],
  );

  return {
    agents,
    defaultId,
    isLoading,
    error,
    refresh: () => mutate(),
    createAgent,
    deleteAgent,
    updateAgent,
  };
}
