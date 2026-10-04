"use client";

// Assembles the roster's view of "what is each agent doing right now" from the
// two signals that already reach the browser, as a plain array.
//
// Neither source is sufficient alone, which is why both are here:
//
//   * `GET /teams/agents` → `CompanyAgent.status` is the DURABLE state,
//     covering Paperclip's autonomous task runs. It survives a reload, but it
//     knows nothing about a webchat turn.
//
//     This hook owns that key's liveness ITSELF — see `ROSTER_SWR` below. It
//     used to lean on `TeamsEventsProvider` revalidating `/teams/agents` off
//     `teams.agent.status`, which held only where that provider was an
//     ancestor; elsewhere, with `SwrProvider` setting `revalidateOnFocus:
//     false` globally, the roster fetched once at mount and never again. A
//     shared hook must not depend on where it happens to be mounted, so the
//     guarantee lives here rather than in an ancestor.
//   * The gateway's `run_active` frame is the LIVE state for exactly that
//     case. The backend resolves it from OpenClaw's own `sessions.changed`
//     (terminal status beats a stale `hasActiveRun` — see
//     `connection_pool._is_session_run_active`) and fans it out to every one
//     of the member's tabs tagged with `agent_id`. So the sidebar can already
//     see a run start on an agent the user is not currently looking at; the
//     chat pane's `useAgentChat` simply drops the frames that aren't its own.
//
// They union: either saying "running" lights the row. Both are best-effort —
// a failed `/teams/agents` read or a missed frame degrades to "nothing to
// report", never to a wrong state.

import { useEffect, useMemo, useSyncExternalStore } from "react";

import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Agent } from "@/hooks/useAgents";
import { rosterStateFor, type RosterEntry } from "@/components/agents/rosterState";

/** The `/teams/agents` fields the roster reads. A narrow structural subset of
 *  `CompanyAgent` so this hook doesn't depend on the full Teams wire shape. */
interface RosterAgentRow {
  id: string;
  status?: string | null;
  role?: string | null;
  seeded?: boolean;
}

/** Sentinel for a `run_active` frame that arrived without a `session_id`. */
const UNKEYED_SESSION = "__unkeyed__";
type ChatMessageSubscriber = ReturnType<typeof useLiveEvents>["onChatMessage"];
type LiveRuns = ReadonlyMap<string, ReadonlySet<string>>;
const EMPTY_LIVE_RUNS: LiveRuns = new Map();

interface LiveRunStore {
  getSnapshot: () => LiveRuns;
  set: (next: LiveRuns) => void;
  subscribe: (listener: () => void) => () => void;
}

function createLiveRunStore(): LiveRunStore {
  let current = EMPTY_LIVE_RUNS;
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => current,
    set: (next) => {
      if (next === current) return;
      current = next;
      for (const listener of listeners) listener();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

// One cache per GatewayProvider connection. Every hook instance still owns
// its React subscription, but a panel mounted after run_active can seed from
// the sidebar's already-observed state. Weak keys prevent one signed-in
// provider lifetime from leaking into the next.
const liveRunsByGateway = new WeakMap<
  ChatMessageSubscriber,
  LiveRunStore
>();

function liveRunStoreFor(onChatMessage: ChatMessageSubscriber): LiveRunStore {
  const existing = liveRunsByGateway.get(onChatMessage);
  if (existing) return existing;
  const created = createLiveRunStore();
  liveRunsByGateway.set(onChatMessage, created);
  return created;
}

/**
 * Liveness for the `/teams/agents` key, owned by this hook so BOTH hosts get
 * it (see the module header for why an ancestor provider was not enough).
 *
 * `SwrProvider`'s docstring names explicit polling as the sanctioned way for a
 * surface that needs liveness to get it. 30s clears the read layer's 15s dedup
 * window, so every tick is a real refetch; on desktop the events provider
 * still delivers status changes immediately and this is only a floor.
 * `revalidateOnFocus` is re-enabled for this one key against the global
 * default -- returning to a backgrounded phone is exactly when the owner wants
 * "who needs me" to be current.
 */
const ROSTER_SWR = { refreshInterval: 30_000, revalidateOnFocus: true };

/**
 * Agent ids with at least one live gateway run.
 *
 * Tracked as a set of session ids PER AGENT, not a single flag. `run_active`
 * carries `{type, active, agent_id, session_id}` and the backend dedupes it
 * per session key, so a flat per-agent boolean was wrong for an agent running
 * two sessions at once: finishing either one cleared the rail while the other
 * was still going. Note there is deliberately no runId keying — unlike
 * `run_ended`, `run_active` carries no runId at all (it is a session-level
 * server-truth flag, which is exactly why `useAgentChat` mirrors it directly
 * rather than gating it on a run).
 */
function useLiveRunAgentIds(): ReadonlySet<string> {
  const { liveConnected, onChatMessage } = useLiveEvents();
  const store = useMemo(() => liveRunStoreFor(onChatMessage), [onChatMessage]);
  const live = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );

  useEffect(() => {
    if (!liveConnected) {
      store.set(EMPTY_LIVE_RUNS);
    }
  }, [liveConnected, store]);

  useEffect(() => {
    return onChatMessage((msg) => {
      const agentId = "agentId" in msg ? msg.agentId : undefined;
      if (typeof agentId !== "string" || !agentId) return;
      // `run_active` is the signal; `run_ended` is the belt-and-braces clear
      // for a dropped/never-delivered `active: false` (a reconnect mid-run
      // would otherwise strand a row on "Working" forever — the same class of
      // bug as the stuck working pill). `useAgentChat` force-closes its own
      // busy flag on `run_ended` for the same reason.
      const active =
        msg.type === "run_active" ? !!msg.active : msg.type === "run_ended" ? false : null;
      if (active === null) return;
      const sessionId =
        "sessionId" in msg && typeof msg.sessionId === "string"
          ? msg.sessionId
          : UNKEYED_SESSION;

      const prev = store.getSnapshot();
      const sessions = prev.get(agentId);
      let current = prev;
      if (active ? !sessions?.has(sessionId) : sessions?.has(sessionId)) {
        const updatedRuns = new Map(prev);
        const updated = new Set(sessions ?? []);
        if (active) updated.add(sessionId);
        else updated.delete(sessionId);
        if (updated.size) updatedRuns.set(agentId, updated);
        else updatedRuns.delete(agentId);
        current = updatedRuns;
      }
      store.set(current);
    });
  }, [onChatMessage, store]);

  // Identity changes only when an agent enters or leaves the live set, so
  // downstream memos genuinely skip work on unrelated frames.
  return useMemo(() => new Set(live.keys()), [live]);
}

/**
 * Build the roster entries for `agents`.
 *
 * CEO-ness comes from the Paperclip `role`, NOT from the container's default
 * agent. `_build_agents_list` picks the `role == "ceo"` agent as the default
 * but silently falls back to index 0 when no agent carries that role, so on a
 * legacy or partially-migrated company `defaultAgentId` is an arbitrary agent.
 * That was harmless when it only hid a delete button; it is not harmless now
 * that the roster pins the row and prints a CEO badge on it. When the roster
 * read hasn't landed, or no agent claims the role, nobody is pinned and nobody
 * is labelled — the badge never outruns the data.
 */
export function useRosterEntries(agents: Agent[]): RosterEntry[] {
  const { read } = useTeamsApi();
  const { data } = read<{ agents?: RosterAgentRow[] }>("/agents", ROSTER_SWR);
  const liveRunAgentIds = useLiveRunAgentIds();
  const rows = data?.agents;

  // SWR keeps `data` referentially stable between revalidations and the live
  // set only changes identity on a real add/remove, so this genuinely skips
  // work on host re-renders driven by unrelated state (dialog targets,
  // `agentActionBusy`, the mobile drawer) — and keeps `entries` referentially
  // stable so the roster's own memos hold.
  return useMemo(() => {
    const byId = new Map((rows ?? []).map((a) => [a.id, a]));
    return agents.map((agent) => {
      const row = byId.get(agent.id);
      const durable = rosterStateFor(row?.status);
      return {
        agent,
        // A live gateway run wins over a durable "idle" — the agent is
        // demonstrably working. It does NOT override any other state: an
        // errored agent that is retrying still needs the human, and a
        // terminated one has no business looking busy.
        state:
          durable === "idle" && liveRunAgentIds.has(agent.id) ? ("running" as const) : durable,
        isCeo: row?.role === "ceo",
        // Backend-authored starter provenance; display names are irrelevant.
        isSeeded: row?.seeded === true,
        rawStatus: row?.status,
      };
    });
  }, [agents, rows, liveRunAgentIds]);
}
