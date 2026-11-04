import { accumulateStreamText, notificationLine, type ChatIncomingMessage } from "@lightbulb/shared";

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
  /** The room on screen, if any: its runs show their own progress, so they never toast. */
  visibleRoomId?: string | null;
  /** Rooms on screen in a side column (session chat, an app's conversation): their runs never toast either. */
  sideRoomIds?: readonly string[];
}

export type RunToastAction =
  // `roomId` is the room a chat run ran in, when the frame named one — the
  // toast's View opens it instead of the agent's DM.
  // `line` (success) is the reply's first line; `issueId`/`errorCode`/`error`
  // (teams) say which task and why.
  | {
      kind: "working" | "success" | "failure"; key: string; agentId: string; source: "chat" | "teams"; roomId?: string;
      line?: string; issueId?: string; errorCode?: string; error?: string;
    }
  // Something is waiting on the user. `message` is resolved here (not in the
  // component) because only the payload knows what is actually being asked;
  // `issueId`, when present, is the task the wait is blocking.
  // `approvalId` (approvals only) lets the caller name the agent and the ask and
  // open Activity on that approval; `agentId` is the requester, when the event says.
  | { kind: "attention"; key: string; message: string; issueId?: string; approvalId?: string; interactionId?: string; agentId?: string }
  | { kind: "dismiss"; key: string };

interface TrackedRun {
  agentId: string;
  sessionId: string | null;
  toastShown: boolean;
  roomId?: string;
  /** The current assistant segment's text: after the last tool call, the reply. */
  text?: string | null;
}

export interface RunToastState {
  chatRuns: Map<string, TrackedRun>;
  teamsRuns: Map<string, TrackedRun>;
  /** Chat runs that already ended: a straggler frame must not raise a fresh "working" toast
   * (prod 2026-09-25: a stopped run's later frames kept "Friday is working…" up). */
  endedChatRuns: Set<string>;
}

export function createRunToastState(): RunToastState {
  return { chatRuns: new Map(), teamsRuns: new Map(), endedChatRuns: new Set() };
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
function chatSuppressed(agentId: string, sessionId: string | null, ctx: RunToastContext, roomId?: string): boolean {
  return (
    (!!roomId && (roomId === ctx.visibleRoomId || !!ctx.sideRoomIds?.includes(roomId))) ||
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

export interface RunFrameIdentity {
  runId: string;
  agentId: string;
  roomId?: string;
}

export function reduceChatFrame(
  state: RunToastState,
  msg: ChatIncomingMessage,
  ctx: RunToastContext,
  identity?: RunFrameIdentity,
): RunToastAction[] {
  if (msg.type === "run_ended") {
    const runId = identity?.runId ?? msg.runId;
    const tracked = state.chatRuns.get(runId);
    state.chatRuns.delete(runId);
    state.endedChatRuns.add(runId);
    // ponytail: drop the oldest past the cap, same bound as the tracked maps.
    if (state.endedChatRuns.size > MAX_TRACKED_RUNS) state.endedChatRuns.delete(state.endedChatRuns.values().next().value as string);
    const agentId = tracked?.agentId ?? identity?.agentId ?? msg.agentId;
    if (!agentId) return [];
    if (msg.outcome === "aborted") {
      return tracked?.toastShown ? [{ kind: "dismiss", key: runId }] : [];
    }
    const sessionId = tracked?.sessionId ?? msg.sessionId ?? null;
    const roomId = tracked?.roomId ?? identity?.roomId;
    if (chatSuppressed(agentId, sessionId, ctx, roomId)) {
      return tracked?.toastShown ? [{ kind: "dismiss", key: runId }] : [];
    }
    const line = msg.outcome === "done" ? notificationLine(tracked?.text) : "";
    return [
      {
        kind: msg.outcome === "done" ? "success" : "failure",
        key: runId,
        agentId,
        source: "chat",
        ...(roomId ? { roomId } : {}),
        ...(line ? { line } : {}),
      },
    ];
  }

  if (!CHAT_START_TYPES.has(msg.type)) return [];
  const runId = identity?.runId ?? ("runId" in msg ? msg.runId : undefined);
  const agentId = identity?.agentId ?? ("agentId" in msg ? msg.agentId : undefined);
  if (!runId || !agentId) return [];
  // A straggler frame after the run ended must not bring it back.
  if (state.endedChatRuns.has(runId)) return [];

  let tracked = state.chatRuns.get(runId);
  const first = !tracked;
  if (!tracked) {
    const sessionId = "sessionId" in msg ? (msg.sessionId ?? null) : null;
    tracked = {
      agentId, sessionId,
      toastShown: !chatSuppressed(agentId, sessionId, ctx, identity?.roomId),
      ...(identity?.roomId ? { roomId: identity.roomId } : {}),
    };
    state.chatRuns.set(runId, tracked);
    evictOldest(state.chatRuns);
  }
  // The current assistant segment: after the last tool call, that is the reply.
  if (msg.type === "chunk") tracked.text = accumulateStreamText(tracked.text ?? null, msg);
  if (msg.type === "tool_start") tracked.text = null;
  return first && tracked.toastShown
    ? [{ kind: "working", key: runId, agentId, source: "chat", ...(tracked.roomId ? { roomId: tracked.roomId } : {}) }]
    : [];
}

export const TEAMS_WORKING_STATUSES = new Set(["queued", "running", "scheduled_retry"]);
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
    const issueId = readString(p.issueId);
    const errorCode = readString(p.errorCode);
    const error = readString(p.error);
    return [
      {
        kind: status === "succeeded" ? "success" : "failure",
        key,
        agentId,
        source: "teams",
        ...(issueId ? { issueId } : {}),
        ...(status !== "succeeded" && errorCode ? { errorCode } : {}),
        ...(status !== "succeeded" && error ? { error } : {}),
      },
    ];
  }
  return [];
}

// Two families of "the ball is in the user's court" ride the generic
// teams.activity.logged event (Paperclip gives neither a first-class live-event
// type); payload.action is the discriminator and payload.details carries the
// specifics.
//
//   issue.thread_interaction_* — a task is PAUSED until a human answers. This is
//     the family that actually fires: production has logged 27 of these and has
//     never once created an approval, and four expired unanswered because no
//     surface said a word.
//   approval.*                 — a board decision is pending.
//
// `revision_requested` resolves the wait as surely as approved/rejected — the
// ball goes back to the agent — so it must clear the toast too. Without it the
// sticky toast outlives the thing it is about. The remaining actions
// (comment_added, work_product_created, the review_path_wakeup_* pair) are the
// user's own moves or too noisy to toast.
const APPROVAL_ATTENTION_ACTIONS = new Set(["approval.created", "approval.resubmitted"]);
const APPROVAL_RESOLVED_ACTIONS = new Set([
  "approval.approved",
  "approval.rejected",
  "approval.revision_requested",
]);
const INTERACTION_CREATED_ACTION = "issue.thread_interaction_created";
const INTERACTION_RESOLVED_ACTIONS = new Set([
  "issue.thread_interaction_accepted",
  "issue.thread_interaction_rejected",
  "issue.thread_interaction_expired",
]);

// Copy is keyed off the payload's own discriminators so the toast says what is
// actually being asked. Both key sets are closed upstream enums
// (ISSUE_THREAD_INTERACTION_KINDS, APPROVAL_TYPES); an unrecognized value falls
// back to the generic line rather than dropping the notification.
const INTERACTION_KIND_COPY: Readonly<Record<string, string>> = {
  request_confirmation: "A task is paused waiting for your confirmation",
  request_checkbox_confirmation: "A task is paused waiting for you to verify a checklist",
  ask_user_questions: "An agent asked you a question and can't continue until you answer",
  suggest_tasks: "An agent suggested follow-up tasks for your review",
};
const APPROVAL_TYPE_COPY: Readonly<Record<string, string>> = {
  hire_agent: "An agent wants to hire a new agent",
  approve_ceo_strategy: "An agent needs your sign-off on its strategy",
  budget_override_required: "An agent needs a budget override approved",
  request_board_approval: "An agent is requesting your approval",
};

// Own-property lookup only: the `in`/index operator walks the prototype chain,
// so a crafted kind ("constructor") would otherwise resolve to a function.
function copyFor(
  map: Readonly<Record<string, string>>,
  key: string | undefined,
  fallback: string,
): string {
  if (!key || !Object.prototype.hasOwnProperty.call(map, key)) return fallback;
  return map[key];
}

function readDetails(p: Record<string, unknown>): Record<string, unknown> {
  const details = p.details;
  return details && typeof details === "object" ? (details as Record<string, unknown>) : {};
}

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
  const details = readDetails(p);

  if (action === INTERACTION_CREATED_ACTION || INTERACTION_RESOLVED_ACTIONS.has(action)) {
    // Keyed on the interaction, not on entityId: entityId is the ISSUE, and one
    // task can hold several interactions at once — keying on the issue would
    // make the second one replace the first's toast.
    const interactionId = readString(details.interactionId);
    if (!interactionId) return [];
    const key = `interaction:${interactionId}`;
    if (action !== INTERACTION_CREATED_ACTION) return [{ kind: "dismiss", key }];
    // An interaction addressed to a specific agent is that agent's to answer;
    // only an unaddressed one is waiting on a human.
    if (readString(details.addresseeAgentId)) return [];
    if (ctx.teamsPanelActive) return [];
    return [
      {
        kind: "attention",
        key,
        message: copyFor(
          INTERACTION_KIND_COPY,
          readString(details.interactionKind),
          "A task is paused waiting for your input",
        ),
        issueId: entityId,
        interactionId,
      },
    ];
  }

  const key = `approval:${entityId}`;
  // Resolution always dismisses (toast.dismiss on an unknown id is a no-op),
  // covering approvals resolved from another device/tab.
  if (APPROVAL_RESOLVED_ACTIONS.has(action)) return [{ kind: "dismiss", key }];
  if (!APPROVAL_ATTENTION_ACTIONS.has(action)) return [];
  if (ctx.teamsPanelActive) return [];
  const agentId = readString(p.agentId);
  return [
    {
      kind: "attention",
      key,
      message: copyFor(
        APPROVAL_TYPE_COPY,
        readString(details.type),
        "An agent is awaiting your approval",
      ),
      approvalId: entityId,
      ...(agentId ? { agentId } : {}),
    },
  ];
}
