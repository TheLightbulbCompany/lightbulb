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
  // Keys the user is currently watching. Two grammars share one set:
  //   `${agentId}`               — bare, agent-level. Suppresses EVERY run for
  //                                that agent (desktop: an agent's chat panel is
  //                                visible, so any of its runs is on-screen).
  //   `${agentId}:${sessionId}`  — per-session (sessionId normalized `?? ""`,
  //                                so General/null == the trailing-colon key).
  // Mobile emits compound-only keys (byte-equivalent to the old per-session
  // selectedAgent/selectedSession/chatViewActive logic); desktop emits both a
  // bare and a compound key per visible chat panel.
  watchedChatKeys: ReadonlySet<string>;
  teamsPanelActive: boolean;
}

export type RunToastAction =
  | { kind: "working" | "success" | "failure"; key: string; agentId: string; source: "chat" | "teams" }
  | { kind: "approval"; key: string }
  | { kind: "dismiss"; key: string };

interface TrackedRun {
  agentId: string;
  sessionId: string | null;
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

// A run the user is already watching never needs a "working" toast.
// Suppression is re-evaluated per event: a run that started watched still gets
// its completion toast if the user has navigated away (the watched key set no
// longer contains its agent/session) by the time it ends. `sessionId` is
// normalized `?? ""` to build the compound key exactly as the ctx builders do,
// so a missing/undefined session_id == null == the "General" trailing-colon
// key. The bare-agent branch lets a visible desktop chat panel suppress every
// run for its agent regardless of the run's session.
function chatSuppressed(agentId: string, sessionId: string | null, ctx: RunToastContext): boolean {
  return (
    ctx.watchedChatKeys.has(agentId) ||
    ctx.watchedChatKeys.has(`${agentId}:${sessionId ?? ""}`)
  );
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
    // `??` falls through when a tracked run's session is null (General); the
    // run_ended frame for that same run also normalizes to null, so the
    // resolved session is identical. The fallback only matters for a
    // never-tracked run (reconnect gap).
    const sessionId =
      tracked?.sessionId ?? ("session_id" in msg ? (msg.session_id ?? null) : null);
    const show = tracked?.toastShown || !chatSuppressed(agentId, sessionId, ctx);
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

  // Captured once at run-start — a run's session doesn't change mid-run.
  const sessionId = "session_id" in msg ? (msg.session_id ?? null) : null;
  const toastShown = !chatSuppressed(agentId, sessionId, ctx);
  state.chatRuns.set(runId, { agentId, sessionId, toastShown });
  evictOldest(state.chatRuns);
  return toastShown
    ? [{ kind: "working", key: runId, agentId, source: "chat" }]
    : [];
}

const TEAMS_WORKING_STATUSES = new Set(["queued", "running", "scheduled_retry"]);
// `interrupted` is the terminal status published on a graceful BFF shutdown
// (spec PR-7) -- without it here a run stuck mid-flight when the BFF
// restarts falls through to `return []` below and leaves its "working"
// toast at a stuck Infinity duration forever.
const TEAMS_FAILURE_STATUSES = new Set(["failed", "timed_out", "interrupted"]);

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
    // sessionId is unused on the teams path (suppression stays keyed on
    // teamsPanelActive only) — set to null purely to satisfy TrackedRun.
    state.teamsRuns.set(runId, { agentId, sessionId: null, toastShown });
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

// Approval lifecycle rides the generic teams.activity.logged event (Paperclip
// has no first-class approval live-event type); payload.action is the
// discriminator. created/resubmitted both mean "the ball is in the user's
// court"; approved/rejected resolve the wait and clear the toast. Other
// actions (revision_requested, comment_added, work_product_created) are the
// user's own moves or too noisy to toast.
const APPROVAL_ATTENTION_ACTIONS = new Set(["approval.created", "approval.resubmitted"]);
const APPROVAL_RESOLVED_ACTIONS = new Set(["approval.approved", "approval.rejected"]);

export function reduceTeamsActivityEvent(
  event: string,
  payload: unknown,
  ctx: RunToastContext,
): RunToastAction[] {
  if (event !== "teams.activity.logged") return [];
  const p = (payload ?? {}) as Record<string, unknown>;
  const action = readString(p.action);
  const entityId = readString(p.entityId);
  if (!action || !entityId) return [];
  const key = `approval:${entityId}`;
  // Resolution always dismisses (toast.dismiss on an unknown id is a no-op),
  // covering approvals resolved from another device/tab.
  if (APPROVAL_RESOLVED_ACTIONS.has(action)) return [{ kind: "dismiss", key }];
  if (!APPROVAL_ATTENTION_ACTIONS.has(action)) return [];
  if (ctx.teamsPanelActive) return [];
  return [{ kind: "approval", key }];
}
