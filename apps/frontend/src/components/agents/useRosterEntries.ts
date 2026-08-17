"use client";

// Assembles the roster's view of "what is each agent doing right now" from the
// two signals that already reach the browser, and hands `AgentRoster` a plain
// array. Kept out of the component file so the roster itself stays a pure
// render of its props.
//
// Neither source is sufficient alone, which is why both are here:
//
//   * `GET /teams/agents` → `CompanyAgent.status` is the DURABLE state —
//     idle / running / error / paused / terminated, covering Paperclip's
//     autonomous task runs. It survives a reload, and `TeamsEventsProvider`
//     already revalidates its SWR key off `teams.agent.status` events. But it
//     knows nothing about a webchat turn.
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

import { useEffect, useState } from "react";

import { useGateway } from "@/hooks/useGateway";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Agent } from "@/hooks/useAgents";
import { rosterStateFor, type RosterEntry } from "@/components/agents/AgentRoster";

/**
 * Agent ids with a live gateway run.
 *
 * ponytail: one flag per agent, not a set of session keys. `run_active` is
 * deduped per session upstream, so an agent running two sessions at once and
 * finishing one would clear the flag early. Track sessions if that case ever
 * matters; a briefly-missing rail is the failure mode, which is the harmless
 * direction.
 */
function useLiveRunAgentIds(): ReadonlySet<string> {
  const { onChatMessage } = useGateway();
  const [live, setLive] = useState<ReadonlySet<string>>(() => new Set<string>());

  useEffect(() => {
    return onChatMessage((msg) => {
      const agentId = "agent_id" in msg ? msg.agent_id : undefined;
      if (!agentId) return;
      // `run_active` is the signal; `run_ended` is the belt-and-braces clear
      // for a dropped/never-delivered `active: false` (a reconnect mid-run
      // would otherwise strand a row on "Working" forever — the same class of
      // bug as the stuck working pill).
      const next =
        msg.type === "run_active" ? msg.active : msg.type === "run_ended" ? false : null;
      if (next === null) return;
      setLive((prev) => {
        if (prev.has(agentId) === next) return prev;
        const updated = new Set(prev);
        if (next) updated.add(agentId);
        else updated.delete(agentId);
        return updated;
      });
    });
  }, [onChatMessage]);

  return live;
}

/**
 * Build the roster entries for `agents`.
 *
 * `defaultAgentId` identifies Friday: the backend marks the `role: "ceo"`
 * Paperclip agent as the container config's default (see
 * `containers/config.py` — `default_index` picks the ceo, falling back to the
 * first agent), so it is the same agent the rest of the app already treats as
 * the front door. No second lookup needed.
 */
export function useRosterEntries(agents: Agent[], defaultAgentId?: string): RosterEntry[] {
  const { read } = useTeamsApi();
  // Same SWR key ChatLayout's pause/resume control already reads, so on the
  // mobile shell this shares one cached response rather than adding a request.
  const { data } = read<{ agents?: { id: string; status?: string | null }[] }>("/agents");
  const liveRunAgentIds = useLiveRunAgentIds();

  const statuses = new Map((data?.agents ?? []).map((a) => [a.id, a.status]));
  return agents.map((agent) => {
    const durable = rosterStateFor(statuses.get(agent.id));
    return {
      agent,
      // A live gateway run wins over a durable "idle" — the agent is demonstrably
      // working. It does NOT override "attention": an errored agent that is
      // retrying still needs the human, and that is the more urgent fact.
      state: durable === "idle" && liveRunAgentIds.has(agent.id) ? "running" : durable,
      isCeo: agent.id === defaultAgentId,
    };
  });
}
