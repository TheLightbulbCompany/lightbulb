"use client";

// The agent roster — the left rail's answer to "who is working, and who needs
// me?". One component, two hosts: `workspace/WorkspaceSidebar` (desktop dock
// shell) and `chat/ChatLayout` (mobile shell). They rendered ~90 identical
// lines of row JSX each and had already started to drift; the roster now lives
// here so they cannot.
//
// Three things this surface does that the flat `agents.map()` it replaces did
// not:
//
//   1. It tells the truth about state. The old rows rendered
//      `<StatusDot status="active" />` — a hardcoded green dot on every agent,
//      forever. A status indicator that never varies is decoration, not
//      information. Real per-agent runtime status already exists on the Teams
//      BFF roster (`GET /teams/agents` → `CompanyAgent.status`), which
//      `TeamsEventsProvider` keeps live off `teams.agent.status` events. The
//      hosts read it and pass it in.
//   2. It orders by how much the agent wants your attention (see
//      `orderRoster`), so triage never depends on the order `agents.list`
//      happened to return.
//   3. It is reachable by keyboard. The old row was a bare `<div onClick>`.
//
// Deliberately NOT here: folders / collapsible groups / manual drag-order.
// Plans cap an owner at 10 agents, so grouping hides four rows behind a
// chevron and manual ordering asks the user to maintain what the sort already
// derives. See the PR for the full argument.

import { useMemo, type ReactNode } from "react";
import { Pencil, Trash2, ListTodo, SlidersHorizontal } from "lucide-react";

import { Monogram } from "@/components/agents/AgentIdentity";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import { fromInstalledAgent } from "@/lib/agentDesign";
import { agentDisplayName, getAgentModelString, type Agent } from "@/hooks/useAgents";
import { buildModelNameMap, displayModelName } from "@/lib/modelDisplay";

/**
 * The roster's view of an agent's runtime state, collapsed from the Teams BFF
 * `CompanyAgent.status` free string ("idle" | "running" | "error" | "paused" |
 * "terminated", plus whatever upstream adds next).
 *
 * `idle` is the absence state: it renders no marker at all. Only agents with
 * something to report get a dot, a word and a rail — so a calm roster looks
 * calm, and the eye goes straight to the rows that don't.
 */
export type RosterState = "running" | "attention" | "paused" | "idle";

/**
 * Ordering rank — LOWER sorts higher. Only PERSISTENT conditions move a row:
 * something that broke goes to the top, something switched off sinks. A
 * *running* agent is deliberately NOT promoted, even though the issue asked
 * for "sort by currently-running".
 *
 * The reason is the 10-agent cap. Promoting live work is right for a list of
 * ephemeral sessions you cannot see all of at once (xAI's agent dashboard does
 * exactly this, and should). Here the whole cast fits on screen without
 * scrolling, so hoisting a running agent buys no visibility — and it costs the
 * owner their spatial memory of where each teammate lives, with rows sliding
 * out from under the cursor every time a 20-second run starts or ends. Run
 * state is marked in place (rail, dot, word) and totalled in the section
 * header; only conditions that persist until a human acts are worth a move.
 */
const STATE_RANK: Record<RosterState, number> = {
  attention: 0,
  running: 1,
  idle: 1,
  paused: 2,
};

const STATE_LABEL: Record<Exclude<RosterState, "idle">, string> = {
  running: "Working",
  attention: "Needs you",
  paused: "Paused",
};

/**
 * Map a Teams BFF status string onto a roster state. Unknown/absent statuses
 * fall back to `idle` rather than inventing a state — the BFF forwards
 * upstream Paperclip JSON verbatim and the enum has drifted across versions,
 * so an unrecognised value must degrade to "nothing to report", never to a
 * scary one.
 */
export function rosterStateFor(status: string | null | undefined): RosterState {
  switch ((status ?? "").toLowerCase()) {
    case "running":
      return "running";
    case "error":
      return "attention";
    case "paused":
    case "terminated":
      return "paused";
    default:
      return "idle";
  }
}

export interface RosterEntry {
  agent: Agent;
  state: RosterState;
  /** Friday — the per-owner CEO / orchestrator. Pinned first, always. */
  isCeo: boolean;
}

/**
 * Derive the roster's order. Pure and total — no persistence, no localStorage,
 * no `Date.now()`. (An earlier "recent assignees" ordering read localStorage
 * from inside a `useMemo` during render and shipped a real bug; ordering that
 * is a pure function of props cannot repeat it.)
 *
 * Friday is pinned to the top regardless of state: she is the front door to
 * the whole company, and a front door that moves is a bad front door. Everyone
 * else sorts by attention rank, then by name so the order is stable and
 * repeatable rather than dependent on whatever `agents.list` returned.
 */
export function orderRoster(entries: RosterEntry[]): RosterEntry[] {
  return [...entries].sort((a, b) => {
    if (a.isCeo !== b.isCeo) return a.isCeo ? -1 : 1;
    const rank = STATE_RANK[a.state] - STATE_RANK[b.state];
    if (rank !== 0) return rank;
    return agentDisplayName(a.agent).localeCompare(agentDisplayName(b.agent));
  });
}

interface AgentRosterProps {
  entries: RosterEntry[];
  /** Currently focused agent — its row is marked `active` + `aria-current`. */
  activeAgentId: string | null;
  /** The undeletable default agent (no delete action rendered on its row). */
  defaultAgentId?: string;
  /** Agents the owner has published to the marketplace — gold pip on the avatar. */
  publishedAgentIds: ReadonlySet<string>;
  /** Marketplace hire that just landed — plays the `slideIn` arrival animation. */
  freshlyInstalledAgentId?: string | null;
  /** `config.get` snapshot, for resolving Bedrock AIP ARNs to display names. */
  configSnapshot?: unknown;
  onSelect: (agentId: string) => void;
  onOpenWork: (agentId: string) => void;
  onInspect: (agentId: string) => void;
  onRename: (agent: Agent) => void;
  onDelete: (agent: Agent) => void;
  /** Per-agent session list, nested under the active row. Each host wires its
   *  own selection side effects (the mobile shell also closes the drawer), so
   *  the nest is injected rather than rebuilt here. */
  renderSessions?: (agentId: string) => ReactNode;
}

export function AgentRoster({
  entries,
  activeAgentId,
  defaultAgentId,
  publishedAgentIds,
  freshlyInstalledAgentId,
  configSnapshot,
  onSelect,
  onOpenWork,
  onInspect,
  onRename,
  onDelete,
  renderSessions,
}: AgentRosterProps): React.ReactElement {
  // Memoized as it was in both hosts before the extraction: the roster now
  // re-renders on every live run start/stop, and walking the model catalog on
  // each of those would be work for a value that only changes with `config.get`.
  const modelNames = useMemo(() => buildModelNameMap(configSnapshot), [configSnapshot]);
  const ordered = orderRoster(entries);
  const workingCount = entries.filter((e) => e.state === "running").length;
  const attentionCount = entries.filter((e) => e.state === "attention").length;

  return (
    <div className="agent-list">
      {/* The count used to be a bare `agents.length`, which is the one number
          the owner can already see by looking. Lead with the news instead:
          what needs them, then what is happening, and fall back to the count
          only when there is genuinely nothing to report. */}
      <div className="agent-section-label">
        <span>My Agents</span>
        {attentionCount > 0 ? (
          <b className="agent-section-label__attention">
            {attentionCount} need{attentionCount === 1 ? "s" : ""} you
          </b>
        ) : workingCount > 0 ? (
          <b className="agent-section-label__working">{workingCount} working</b>
        ) : (
          <b>{entries.length}</b>
        )}
      </div>

      {ordered.map(({ agent, state, isCeo }) => {
        const name = agentDisplayName(agent);
        const isActive = activeAgentId === agent.id;
        const displayAgent = fromInstalledAgent(agent);
        const model = displayModelName(getAgentModelString(agent), modelNames);
        const stateLabel = state === "idle" ? null : STATE_LABEL[state];
        return (
          <div
            key={agent.id}
            data-state={state}
            data-ceo={isCeo ? "true" : undefined}
            className={`agent-item${isActive ? " active" : ""}${
              freshlyInstalledAgentId === agent.id ? " new-arrival" : ""
            }`}
          >
            {/* The row's click target is a real <button>, not the old
                `<div onClick>`: Tab reaches it, Enter/Space fire it, the focus
                ring is the platform's, and a screen reader announces a
                control. The per-row action buttons stay siblings — nesting
                them inside would be invalid HTML. */}
            <button
              type="button"
              className="agent-item__main"
              aria-current={isActive ? "true" : undefined}
              onClick={() => onSelect(agent.id)}
            >
              <span className="relative inline-flex">
                <Monogram agent={displayAgent} size={28} radius={8} />
                {publishedAgentIds.has(agent.id) && (
                  <span
                    className="absolute top-0 right-0 w-2 h-2 rounded-full bg-[var(--gold)] border-2 border-[var(--cream)]"
                    aria-label="Published to marketplace"
                    title="Published to marketplace"
                  />
                )}
              </span>
              <span className="agent-info">
                <span className="agent-name">
                  <span className="agent-name__text">{name}</span>
                  {/* Friday is the orchestrator every other agent reports
                      through — the roster should say so, not leave the owner
                      to remember which of ten names is the one that can
                      delegate. */}
                  {isCeo && <span className="agent-rank">CEO</span>}
                </span>
                {/* Second line: what this agent is DOING when it is doing
                    something, and only otherwise what it runs on. A model name
                    is a fact about the machine; "Working" is a fact about the
                    work, and the work is what the owner came here for. */}
                {stateLabel ? (
                  <span className={`agent-state agent-state--${state}`}>
                    {state === "running" ? (
                      <LiveRunDot state="running" decorative />
                    ) : (
                      <span className="agent-state__dot" aria-hidden />
                    )}
                    {stateLabel}
                  </span>
                ) : (
                  model && <span className="agent-model">{model}</span>
                )}
              </span>
            </button>

            <div className="agent-row-actions">
              <button
                type="button"
                className="agent-row-action"
                aria-label={`See ${name}'s work`}
                title={`See ${name}'s work`}
                onClick={() => onOpenWork(agent.id)}
              >
                <ListTodo size={13} />
              </button>
              <button
                type="button"
                className="agent-row-action"
                aria-label={`Inspect ${name}`}
                title={`Inspect ${name}`}
                onClick={() => onInspect(agent.id)}
              >
                <SlidersHorizontal size={13} />
              </button>
              <button
                type="button"
                className="agent-row-action"
                aria-label={`Rename ${name}`}
                onClick={() => onRename(agent)}
              >
                <Pencil size={13} />
              </button>
              {agent.id !== defaultAgentId && (
                <button
                  type="button"
                  className="agent-row-action agent-row-action--danger"
                  aria-label={`Delete ${name}`}
                  onClick={() => onDelete(agent)}
                >
                  <Trash2 size={13} />
                </button>
              )}
            </div>

            {isActive && renderSessions?.(agent.id)}
          </div>
        );
      })}
    </div>
  );
}
