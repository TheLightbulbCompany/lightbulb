"use client";

import { useCallback } from "react";
import { capture } from "@/lib/analytics";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

/**
 * The gateway's `agents.list` RPC returns the agent's `model` field in one
 * of two shapes depending on whether the agent has an override:
 *
 *  - legacy flat string: `"amazon-bedrock/qwen.qwen3-vl-235b-a22b"`
 *  - structured object:  `{ primary: "amazon-bedrock/...", fallbacks: [...] }`
 *
 * Pre-OpenClaw-4.5 flattened this to a string in RPC responses; 4.5 returns
 * the structured shape as-is, which blew up anywhere on the frontend that
 * was calling `agent.model.split(...)` directly. Call sites MUST use
 * `getAgentModelString` to normalize.
 */
export type AgentModelRef = string | { primary?: string; fallbacks?: string[] };

export interface Agent {
  id: string;
  name?: string;
  identity?: { name?: string; emoji?: string; avatar?: string };
  model?: AgentModelRef;
}

/** Normalize an `Agent.model` field to a plain string (or undefined). */
export function getAgentModelString(agent: Pick<Agent, "model"> | undefined): string | undefined {
  const model = agent?.model;
  if (typeof model === "string") return model.trim() || undefined;
  if (model && typeof model === "object") return model.primary?.trim() || undefined;
  return undefined;
}

/** Resolve the human-readable label for an agent. Prefers ``identity.name``
 * (the user-visible name OpenClaw renders in its own UI), falls back to the
 * top-level ``name`` field, and only as a last resort surfaces the raw
 * ``id`` (e.g. ``agent_e7668b0dd6a6`` from a freshly-deployed catalog
 * template before the user has renamed it). */
export function agentDisplayName(
  agent: Pick<Agent, "id" | "name" | "identity">,
): string {
  return agent.identity?.name || agent.name || agent.id;
}

interface AgentsListResponse {
  defaultId?: string;
  agents?: Agent[];
}

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
