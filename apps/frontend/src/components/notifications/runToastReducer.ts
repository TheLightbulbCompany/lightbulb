import type { ChatIncomingMessage } from "@/hooks/useGateway";

/**
 * Pure frame→toast-action logic for RunNotifications. The component owns
 * subscriptions and toast side effects; everything decidable from data is
 * decided here so it can be tested without React.
 *
 * State maps mutate in place (Map insertion order doubles as the eviction
 * queue); the returned action list is the only output the caller executes.
 */

export interface RunToastContext {
  selectedAgentId: string | null;
  chatViewActive: boolean;
  teamsPanelActive: boolean;
}

export type RunToastAction =
  | { kind: "working" | "success" | "failure"; key: string; agentId: string; source: "chat" | "teams" }
  | { kind: "dismiss"; key: string };

interface TrackedRun {
  agentId: string;
  toastShown: boolean;
}

export interface RunToastState {
  chatRuns: Map<string, TrackedRun>;
  teamsRuns: Map<string, TrackedRun>;
}

export function createRunToastState(): RunToastState {
  return { chatRuns: new Map(), teamsRuns: new Map() };
}

export const MAX_TRACKED_RUNS = 200;

// A run the user is already watching (its agent selected, chat visible) never
// needs a "working" toast. Suppression is re-evaluated per event: a run that
// started watched still gets its completion toast if the user has navigated
// away by the time it ends.
function chatSuppressed(agentId: string, ctx: RunToastContext): boolean {
  return ctx.chatViewActive && ctx.selectedAgentId === agentId;
}

function evictOldest(map: Map<string, TrackedRun>): void {
  while (map.size > MAX_TRACKED_RUNS) {
    const oldest = map.keys().next().value;
    if (oldest === undefined) return;
    map.delete(oldest);
  }
}

// Streaming frame types that indicate a run is producing output. `done` and
// `error` are deliberately absent: only `run_ended` is authoritative (a `done`
// can be the interim final that closes a tool-approval bracket).
const CHAT_START_TYPES = new Set<ChatIncomingMessage["type"]>([
  "chunk",
  "thinking",
  "tool_start",
  "tool_end",
  "tool_error",
  "tool_progress",
  "command_output",
  "agent_activity",
]);

export function reduceChatFrame(
  state: RunToastState,
  msg: ChatIncomingMessage,
  ctx: RunToastContext,
): RunToastAction[] {
  if (msg.type === "run_ended") {
    const tracked = state.chatRuns.get(msg.runId);
    state.chatRuns.delete(msg.runId);
    const agentId = tracked?.agentId ?? msg.agent_id;
    if (!agentId) return [];
    if (msg.outcome === "aborted") {
      return tracked?.toastShown ? [{ kind: "dismiss", key: msg.runId }] : [];
    }
    const show = tracked?.toastShown || !chatSuppressed(agentId, ctx);
    if (!show) return [];
    return [
      {
        kind: msg.outcome === "done" ? "success" : "failure",
        key: msg.runId,
        agentId,
        source: "chat",
      },
    ];
  }

  if (!CHAT_START_TYPES.has(msg.type)) return [];
  const runId = "runId" in msg ? msg.runId : undefined;
  const agentId = "agent_id" in msg ? msg.agent_id : undefined;
  if (!runId || !agentId) return [];
  if (state.chatRuns.has(runId)) return [];

  const toastShown = !chatSuppressed(agentId, ctx);
  state.chatRuns.set(runId, { agentId, toastShown });
  evictOldest(state.chatRuns);
  return toastShown
    ? [{ kind: "working", key: runId, agentId, source: "chat" }]
    : [];
}

const TEAMS_WORKING_STATUSES = new Set(["queued", "running", "scheduled_retry"]);
const TEAMS_FAILURE_STATUSES = new Set(["failed", "timed_out"]);

function readString(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

export function reduceTeamsRunEvent(
  state: RunToastState,
  event: string,
  payload: unknown,
  ctx: RunToastContext,
): RunToastAction[] {
  if (event !== "teams.heartbeat.run.queued" && event !== "teams.heartbeat.run.status") {
    return [];
  }
  const p = (payload ?? {}) as Record<string, unknown>;
  const runId = readString(p.runId);
  if (!runId) return [];
  const agentId = readString(p.agentId) ?? "";
  const key = `teams:${runId}`;
  const status =
    event === "teams.heartbeat.run.queued" ? "queued" : readString(p.status);
  if (!status) return [];

  if (TEAMS_WORKING_STATUSES.has(status)) {
    if (state.teamsRuns.has(runId)) return [];
    const toastShown = !ctx.teamsPanelActive;
    state.teamsRuns.set(runId, { agentId, toastShown });
    evictOldest(state.teamsRuns);
    return toastShown
      ? [{ kind: "working", key, agentId, source: "teams" }]
      : [];
  }

  const tracked = state.teamsRuns.get(runId);
  state.teamsRuns.delete(runId);
  if (status === "cancelled") {
    return tracked?.toastShown ? [{ kind: "dismiss", key }] : [];
  }
  if (status === "succeeded" || TEAMS_FAILURE_STATUSES.has(status)) {
    const show = tracked?.toastShown || !ctx.teamsPanelActive;
    if (!show) return [];
    return [
      {
        kind: status === "succeeded" ? "success" : "failure",
        key,
        agentId,
        source: "teams",
      },
    ];
  }
  return [];
}
