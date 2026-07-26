import { useCallback, useEffect, useMemo, useRef } from "react";

import type { GatewayConnectionValue } from "../gateway/useGatewayConnection";
import { useGatewayRpc, useGatewayRpcMutation, type GatewayRpcSource } from "./useGatewayRpc";
import type { AgentsListResponse } from "./agents";

/**
 * The agent roster, and the only correct way to ask for it.
 *
 * This hook exists because "call agents.list" is NOT the whole job — the
 * roster has two failure modes that every raw call site has to re-solve, and
 * historically didn't:
 *
 *  1. The onboarding EMPTY-ROSTER LATCH (see rosterRefreshInterval).
 *  2. Staleness after the container becomes reachable (see the event effect).
 *
 * It was web-only until the mobile roster shipped five raw
 * `useGatewayRpc(gateway, "agents.list")` call sites carrying neither fix,
 * which is how a waking container left a TestFlight user staring at a red
 * "Couldn't load agents" card. Both clients now share this. New roster
 * consumers call useAgents — never agents.list directly.
 */

// The slice of the gateway this hook needs: the RPC surface plus the
// connected flag for the reconnect rising edge.
export interface AgentsGatewaySource extends GatewayRpcSource {
  isConnected: GatewayConnectionValue["isConnected"];
}

/** Exactly the events this hook emits — named so a caller with a closed
 *  event union (web's AnalyticsEvent) stays assignable, and so adding an
 *  emit here fails the web build until its union covers it. */
export type AgentsAnalyticsEvent =
  | "agent_created"
  | "agent_deleted"
  | "agent_model_changed";

export interface UseAgentsOptions {
  /**
   * Optional analytics sink for the mutations. Web wires PostHog; mobile has
   * no client analytics (the backend emits its own events), so it passes
   * nothing. Routed through a ref, so an inline lambda here does not churn
   * the mutation callbacks.
   */
  capture?: (event: AgentsAnalyticsEvent, props?: Record<string, unknown>) => void;
}

// The onboarding empty-roster latch, fixed at the fetch altitude. While the
// container is still provisioning, `agents.list` resolves empty; keep the key
// polling until Friday/Atlas land, then stop so steady-state never polls. This
// is SWR's *functional* refreshInterval — SWR re-evaluates it against the
// latest cached data on every poll cycle (empty/undefined → keep polling,
// populated → 0). Every provisioned owner has Friday/Atlas, so a persistently
// empty roster is precisely the latch this heals.
// NOT tunable below useGatewayRpc's `dedupingInterval` (10s): SWR runs
// refreshInterval revalidations WITH_DEDUPE, so a shorter period silently
// drops most of its ticks — a "3s" poll actually healed at ~10s. This is the
// BACKSTOP anyway; the become-reachable pushes below are the fast path.
const EMPTY_ROSTER_POLL_MS = 10_000;
function rosterRefreshInterval(latest: AgentsListResponse | undefined): number {
  return (latest?.agents?.length ?? 0) === 0 ? EMPTY_ROSTER_POLL_MS : 0;
}

export function useAgents(gateway: AgentsGatewaySource, opts?: UseAgentsOptions) {
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
    gateway,
    "agents.list",
    undefined,
    { refreshInterval: rosterRefreshInterval },
  );
  const callRpc = useGatewayRpcMutation(gateway);
  const { onEvent, isConnected } = gateway;

  const captureRef = useRef(opts?.capture);
  captureRef.current = opts?.capture;
  const track = useCallback(
    (event: AgentsAnalyticsEvent, props?: Record<string, unknown>) => {
      captureRef.current?.(event, props);
    },
    [],
  );

  // Memoized: a fresh `[]` every render would re-fire any consumer effect or
  // useMemo keyed on the roster (the mobile first-run redirect is one), which
  // is why the mobile control panel had already hand-rolled this locally.
  const agents = useMemo(() => data?.agents ?? [], [data]);
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
  //  3. WS reconnect rising edge — a fresh socket means every
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
        track("agent_created", {
          agent_id: normalizedId,
          agent_name: params.name,
          workspace,
        });
      } finally {
        await mutate();
      }
    },
    [callRpc, mutate, track],
  );

  const deleteAgent = useCallback(
    async (agentId: string) => {
      await callRpc("agents.delete", { agentId, deleteFiles: true });
      track("agent_deleted", { agent_id: agentId });
      mutate();
    },
    [callRpc, mutate, track],
  );

  const updateAgent = useCallback(
    async (agentId: string, updates: { model?: string; name?: string }) => {
      await callRpc("agents.update", { agentId, ...updates });
      if (updates.model) {
        track("agent_model_changed", { agent_id: agentId, model: updates.model });
      }
      mutate();
    },
    [callRpc, mutate, track],
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
