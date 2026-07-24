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

// The onboarding empty-roster latch, fixed at the fetch altitude. While the
// container is still provisioning, `agents.list` resolves empty; keep the key
// polling until Friday/Atlas land, then stop so steady-state never polls. This
// is SWR's *functional* refreshInterval — SWR re-evaluates it against the
// latest cached data on every poll cycle (empty/undefined → keep polling,
// populated → 0). Every provisioned owner has Friday/Atlas, so a persistently
// empty roster is precisely the latch this heals.
const EMPTY_ROSTER_POLL_MS = 3000;
function rosterRefreshInterval(latest: AgentsListResponse | undefined): number {
  return (latest?.agents?.length ?? 0) === 0 ? EMPTY_ROSTER_POLL_MS : 0;
}

export function useAgents() {
  // ---- Roster freshness repair (the onboarding empty-roster latch) ----
  //
  // Root cause: during onboarding `agents.list` rejects with "No container
  // provisioned." — which the shared RPC fetcher deliberately resolves to
  // `undefined` as a 404-equivalent SUCCESS (many useGatewayRpc consumers rely
  // on `data === undefined` meaning "no container yet, render empty not
  // error"). SWR then settles the key permanently: revalidateOnFocus is off,
  // there's no error to retry, and no gateway event is named "agents.*", so
  // nothing re-asks and the roster latches at 0 until a manual reload.
  //
  // Fix at the right altitude: keep the not-ready key revalidating via SWR's
  // own refreshInterval (see rosterRefreshInterval — active while empty,
  // self-disabling once agents land), not a bespoke timer bolted on here.
  const { data, error, isLoading, mutate } = useGatewayRpc<AgentsListResponse>(
    "agents.list",
    undefined,
    { refreshInterval: rosterRefreshInterval },
  );
  const callRpc = useGatewayRpcMutation();
  const { onEvent, isConnected } = useGateway();

  const agents = data?.agents ?? [];
  const defaultId = data?.defaultId;

  // Instant-hydration path: revalidate the moment the runtime becomes
  // reachable, ahead of the poll above. These are throttle-safe WS pushes
  // (delivered on message-receipt, not a self-rescheduling SWR chain), so they
  // ALSO cover an occluded tab whose refreshInterval chain Chrome throttled
  // dead — the same occlusion-throttle #1398 and the Teams board keeper
  // document. Because a populated roster always coincides with one of these
  // pushes, that throttle death cannot re-latch here (which is why no raw
  // fixed-cadence backstop is needed):
  //  1. `status_change` state=HEALTHY — GatewayConnection pushes this the
  //     instant its container handshake completes (same ready signal
  //     ProvisioningStepper rides for /container/status).
  //  2. `onboarding_status_changed` — the durable-write status push; fires
  //     when runtime_phase/chat readiness flips on the composed contract.
  //  3. Browser-WS reconnect rising edge — a fresh socket means every
  //     settled-empty/errored-out key predates the current connection.
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
