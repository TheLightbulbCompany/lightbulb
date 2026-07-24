"use client";

import { useCallback, useEffect, useRef } from "react";
import {
  agentDisplayName,
  getAgentModelString,
  type Agent,
  type AgentModelRef,
  type AgentsListResponse,
} from "@isol8/shared";
import { capture } from "@/lib/analytics";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

// Agent types + pure helpers moved to @isol8/shared (Phase 1 extraction);
// re-exported so feature code keeps importing them from here. The useAgents
// hook itself stays web-side — its mutations couple to PostHog analytics.
export { agentDisplayName, getAgentModelString };
export type { Agent, AgentModelRef };

// Raw-interval cadence for the empty-roster backstop below. Matches the RPC
// hook's dedupingInterval so overlapping mounts collapse to one fetch.
const EMPTY_ROSTER_BACKSTOP_MS = 10_000;

export function useAgents() {
  const { data, error, isLoading, mutate } =
    useGatewayRpc<AgentsListResponse>("agents.list");
  const callRpc = useGatewayRpcMutation();
  const { onEvent, isConnected, isParked } = useGateway();

  const agents = data?.agents ?? [];
  const defaultId = data?.defaultId;

  // ---- Roster freshness repair (the onboarding empty-roster latch) ----
  //
  // During onboarding the container doesn't exist yet, so `agents.list`
  // rejects with "No container provisioned." — which the shared RPC fetcher
  // deliberately resolves to `undefined` as a SUCCESS (404-equivalent). SWR
  // then considers the key settled: no error-retry, no refreshInterval, no
  // revalidateOnFocus, and no gateway event named "agents.*" ever arrives —
  // so the roster latches at 0 agents until a manual reload. Other startup
  // errors ("Container is starting up…") exhaust the fixed-cadence retry
  // budget (~5min) with the same latched outcome. Same disease family as the
  // throttle-killed SWR chains useOnboardingStatus documents.
  //
  // Repair = revalidate on every signal that the runtime just became
  // reachable, plus a raw-interval backstop while the roster is still empty:
  //  1. `status_change` state=HEALTHY — the backend pool pushes this at the
  //     exact moment its container handshake completes (same instant-ready
  //     push ProvisioningStepper rides for /container/status).
  //  2. `onboarding_status_changed` — the durable-write status push; fires
  //     when runtime_phase/chat readiness flips on the composed contract.
  //  3. Browser-WS reconnect rising edge — a fresh socket means every
  //     settled-empty/errored-out key predates the current connection.
  //  4. Empty-roster backstop — a raw setInterval (independent of SWR's
  //     promise chain, per the established backstop pattern) that re-asks
  //     while mounted+connected and the roster is still empty. Self-stops
  //     the moment agents arrive; every provisioned owner has Friday/Atlas,
  //     so a persistently empty roster is precisely the latch this heals.
  const mutateRef = useRef(mutate);
  mutateRef.current = mutate;

  useEffect(() => {
    return onEvent((event, payload) => {
      if (event === "onboarding_status_changed") {
        void mutateRef.current();
        return;
      }
      if (event !== "status_change") return;
      if ((payload as { state?: string } | null)?.state !== "HEALTHY") return;
      void mutateRef.current();
    });
  }, [onEvent]);

  const prevConnectedRef = useRef(isConnected);
  useEffect(() => {
    const wasConnected = prevConnectedRef.current;
    prevConnectedRef.current = isConnected;
    if (!wasConnected && isConnected) void mutateRef.current();
  }, [isConnected]);

  const rosterEmpty = (data?.agents?.length ?? 0) === 0;
  useEffect(() => {
    if (!rosterEmpty || !isConnected || isParked) return;
    const id = setInterval(() => {
      void mutateRef.current();
    }, EMPTY_ROSTER_BACKSTOP_MS);
    return () => clearInterval(id);
  }, [rosterEmpty, isConnected, isParked]);

  const createAgent = useCallback(
    // OpenClaw's agents.create requires a non-empty `workspace` string
    // (it does NOT inherit agents.defaults.workspace at create time).
    // When the caller omits `workspace`, we fill in the same path the
    // default would resolve to at runtime — .openclaw/workspace-{id} —
    // so the agent lands on EFS without the caller knowing about path
    // conventions.
    async (params: { name: string; workspace?: string; emoji?: string }) => {
      const normalizedId = params.name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/-+/g, "-")
        .replace(/^-|-$/g, "");
      const workspace = params.workspace ?? `.openclaw/workspace-${normalizedId}`;
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
    // Roster has settled once the RPC has resolved (success OR error) — SWR
    // `data` flips from undefined to a value exactly once. Consumers gating
    // "agent no longer exists" must wait for this: `isLoading` is false during
    // error-retry cycles and `agents` defaults to [] before the first fetch,
    // so either alone would false-fire against a persisted panel at load.
    isSettled: data !== undefined,
    error,
    refresh: () => mutate(),
    createAgent,
    deleteAgent,
    updateAgent,
  };
}
