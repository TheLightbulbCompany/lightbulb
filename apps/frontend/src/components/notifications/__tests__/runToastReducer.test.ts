import { describe, it, expect } from "vitest";
import type { ChatIncomingMessage } from "@lightbulb/shared";
import {
  createRunToastState,
  reduceChatFrame,
  reduceTeamsRunEvent,
  reduceTeamsActivityEvent,
  MAX_TRACKED_RUNS,
  type RunToastContext,
} from "../runToastReducer";

// New ctx shape: watchedChatKeys is a set of `${agentId}` (bare — agent-level,
// desktop) and `${agentId}:${sessionId ?? ""}` (per-session) entries the user
// is watching. A frame is suppressed iff its bare-agent key OR its
// agent:session key is present. Mobile builds compound-only keys (per-session,
// byte-equivalent to the old selectedAgentId/selectedSessionId/chatViewActive
// logic); a General/null session maps to the trailing-colon key "agent:".
function chatCtx(keys: string[], teamsPanelActive = false): RunToastContext {
  return { watchedChatKeys: new Set(keys), teamsPanelActive };
}

// Mobile watching (agent-sel, General/null) in chat view — the old bgCtx.
const bgCtx: RunToastContext = chatCtx(["agent-sel:"]);

// Omit sessionId entirely when unset, matching the real wire shape of a
// General-session frame (backend never stamps sessionId on 3-part keys), so
// tests exercise the `"sessionId" in msg === false` branch of the reducer.
function chunk(runId: string, agentId?: string, sessionId?: string): ChatIncomingMessage {
  return {
    type: "chunk",
    content: "hi",
    runId,
    agentId: agentId,
    ...(sessionId !== undefined ? { sessionId: sessionId } : {}),
  };
}

function runEnded(
  runId: string,
  outcome: "done" | "error" | "aborted",
  agentId?: string,
  sessionId?: string,
): ChatIncomingMessage {
  return {
    type: "run_ended",
    runId,
    outcome,
    agentId: agentId,
    ...(sessionId !== undefined ? { sessionId: sessionId } : {}),
  };
}

describe("reduceChatFrame — run start", () => {
  it("emits a working action on the first frame of a background run", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("emits nothing on subsequent frames of a tracked run", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx)).toEqual([]);
  });

  it("suppresses the watched agent in chat view but still tracks the run", () => {
    const s = createRunToastState();
    const watched: ChatIncomingMessage = chunk("r1", "agent-sel");
    expect(reduceChatFrame(s, watched, bgCtx)).toEqual([]);
    // terminal while STILL watched: stays silent
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel"), bgCtx)).toEqual([]);
  });

  it("does not suppress the selected agent while a panel is open", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = chatCtx([]); // nothing watched (chat view not active)
    expect(reduceChatFrame(s, chunk("r1", "agent-sel"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("suppresses the watched agent when the run's session matches the active session", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = chatCtx(["agent-sel:s1"]);
    const watched: ChatIncomingMessage = chunk("r1", "agent-sel", "s1");
    expect(reduceChatFrame(s, watched, ctx)).toEqual([]);
    // terminal while STILL matching agent+session: stays silent
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel", "s1"), ctx)).toEqual([]);
  });

  it("suppresses the watched agent when both are on General (null session)", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = chatCtx(["agent-sel:"]);
    const watched: ChatIncomingMessage = chunk("r1", "agent-sel"); // no sessionId -> null
    expect(reduceChatFrame(s, watched, ctx)).toEqual([]);
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel"), ctx)).toEqual([]);
  });

  it("toasts a background run on a DIFFERENT session of the watched agent", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = chatCtx(["agent-sel:s1"]);
    expect(reduceChatFrame(s, chunk("r1", "agent-sel", "s2"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel", "s2"), ctx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-sel", source: "chat", line: "hi" },
    ]);
  });

  it("toasts a background run in a named session while the watched agent view is on General", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = chatCtx(["agent-sel:"]);
    expect(reduceChatFrame(s, chunk("r1", "agent-sel", "s2"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("ignores frames without runId or agentId and passive frames", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, chunk("r1", undefined), bgCtx)).toEqual([]);
    expect(reduceChatFrame(s, { type: "heartbeat" }, bgCtx)).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "run_active", active: true, agentId: "a" }, bgCtx),
    ).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "done", runId: "r1", agentId: "agent-bg" }, bgCtx),
    ).toEqual([]);
  });
});

describe("reduceChatFrame — desktop multi-panel suppression", () => {
  // Desktop builds watchedChatKeys from EVERY visible chat panel: for each
  // visible agent it adds the bare `agentId` (agent-level suppression) plus the
  // `${agentId}:${session}` key for the session that panel is showing. A chat
  // run is suppressed iff its agent's panel is visible — even if that panel is
  // not the single focused tab.

  it("suppresses a run whose agent panel is visible but not the focused tab", () => {
    const s = createRunToastState();
    // Two agents visible in different groups; B is not the focused panel but is
    // the active tab of its group, so its keys are watched.
    const ctx = chatCtx(["A", "A:sa", "B", "B:sb"]);
    expect(reduceChatFrame(s, chunk("r1", "B", "sb"), ctx)).toEqual([]);
    expect(reduceChatFrame(s, runEnded("r1", "done", "B", "sb"), ctx)).toEqual([]);
  });

  it("toasts a run for an agent whose panel is NOT visible", () => {
    const s = createRunToastState();
    const ctx = chatCtx(["A", "A:sa"]); // only A visible
    expect(reduceChatFrame(s, chunk("r1", "C", "sc"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "C", source: "chat" },
    ]);
  });

  it("suppresses ALL sessions of a visible agent via the bare-agent key", () => {
    const s = createRunToastState();
    // On desktop the per-agent chat panel is right there, so a background run on
    // a DIFFERENT session of the visible agent is still suppressed (agent-level),
    // unlike mobile's strict per-session behavior.
    const ctx = chatCtx(["A", "A:sa"]);
    expect(reduceChatFrame(s, chunk("r1", "A", "s-other"), ctx)).toEqual([]);
  });

  it("suppresses a sessionless frame on bare-agent match", () => {
    const s = createRunToastState();
    const ctx = chatCtx(["A", "A:"]); // A visible on General/null
    expect(reduceChatFrame(s, chunk("r1", "A"), ctx)).toEqual([]); // no sessionId -> null
  });
});

describe("reduceChatFrame — terminals (run_ended only)", () => {
  it("flips a shown run to success on outcome done", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "done"), bgCtx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-bg", source: "chat", line: "hi" },
    ]);
  });

  it("flips a shown run to failure on outcome error", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "error"), bgCtx)).toEqual([
      { kind: "failure", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("carries the room a run ran in onto its terminal action", () => {
    const s = createRunToastState();
    const identity = { runId: "r1", agentId: "agent-bg", roomId: "room-1" };
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx, identity);
    expect(reduceChatFrame(s, runEnded("r1", "error"), bgCtx, identity)).toEqual([
      { kind: "failure", key: "r1", agentId: "agent-bg", source: "chat", roomId: "room-1" },
    ]);
  });

  it("dismisses a shown run on outcome aborted", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "aborted"), bgCtx)).toEqual([
      { kind: "dismiss", key: "r1" },
    ]);
  });

  it("a frame arriving after a stopped run ended never brings the working toast back", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    reduceChatFrame(s, runEnded("r1", "aborted"), bgCtx);
    expect(reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx)).toEqual([]);
  });

  it("toasts a never-seen terminal when unsuppressed (reconnect gap)", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, runEnded("r9", "done", "agent-bg"), bgCtx)).toEqual([
      { kind: "success", key: "r9", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("shows completion for a run that started suppressed if the user navigated away", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-sel"), bgCtx); // suppressed at start
    const awayCtx: RunToastContext = chatCtx([]); // chat no longer active
    expect(reduceChatFrame(s, runEnded("r1", "done"), awayCtx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-sel", source: "chat", line: "hi" },
    ]);
  });

  it("shows completion after the user switched to a DIFFERENT session of the same agent", () => {
    // The core per-session behavior: a run on the watched agent+session starts
    // suppressed, then completes while the user is viewing a SECOND session of
    // the same agent (chat still visible). Per-agent suppression would wrongly
    // mute it; per-session must toast.
    const s = createRunToastState();
    const onS1: RunToastContext = chatCtx(["agent-sel:s1"]);
    expect(reduceChatFrame(s, chunk("r1", "agent-sel", "s1"), onS1)).toEqual([]);
    const onS2: RunToastContext = chatCtx(["agent-sel:s2"]);
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel", "s1"), onS2)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-sel", source: "chat", line: "hi" },
    ]);
  });

  it("ignores a never-seen terminal with no agentId", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, runEnded("r9", "done"), bgCtx)).toEqual([]);
  });
});

describe("reduceChatFrame — eviction", () => {
  it("caps tracked runs, evicting oldest first", () => {
    const s = createRunToastState();
    for (let i = 0; i < MAX_TRACKED_RUNS + 5; i++) {
      reduceChatFrame(s, chunk(`r${i}`, "agent-bg"), bgCtx);
    }
    expect(s.chatRuns.size).toBe(MAX_TRACKED_RUNS);
    expect(s.chatRuns.has("r0")).toBe(false);
    expect(s.chatRuns.has(`r${MAX_TRACKED_RUNS + 4}`)).toBe(true);
  });
});

describe("reduceTeamsRunEvent", () => {
  it("emits working on queued when no teams panel is open", () => {
    const s = createRunToastState();
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" }, bgCtx),
    ).toEqual([{ kind: "working", key: "teams:t1", agentId: "pa1", source: "teams" }]);
  });

  it("suppresses working while a teams panel is open, then toasts terminal after leaving", () => {
    const s = createRunToastState();
    const onTeams: RunToastContext = chatCtx(["agent-sel:"], true);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" }, onTeams),
    ).toEqual([]);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t1", agentId: "pa1", status: "succeeded" }, bgCtx),
    ).toEqual([{ kind: "success", key: "teams:t1", agentId: "pa1", source: "teams" }]);
  });

  it("treats running/scheduled_retry as working states without re-emitting", () => {
    const s = createRunToastState();
    reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" }, bgCtx);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t1", agentId: "pa1", status: "running" }, bgCtx),
    ).toEqual([]);
  });

  it("registers a run first seen via a running status event", () => {
    const s = createRunToastState();
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t2", agentId: "pa1", status: "running" }, bgCtx),
    ).toEqual([{ kind: "working", key: "teams:t2", agentId: "pa1", source: "teams" }]);
  });

  it("maps failed and timed_out to failure", () => {
    const s = createRunToastState();
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t3", agentId: "pa1", status: "failed" }, bgCtx),
    ).toEqual([{ kind: "failure", key: "teams:t3", agentId: "pa1", source: "teams" }]);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t4", agentId: "pa1", status: "timed_out" }, bgCtx),
    ).toEqual([{ kind: "failure", key: "teams:t4", agentId: "pa1", source: "teams" }]);
  });

  it("dismisses a shown run on cancelled, silently drops an unshown one", () => {
    const s = createRunToastState();
    reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t5", agentId: "pa1" }, bgCtx);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t5", agentId: "pa1", status: "cancelled" }, bgCtx),
    ).toEqual([{ kind: "dismiss", key: "teams:t5" }]);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t6", agentId: "pa1", status: "cancelled" }, bgCtx),
    ).toEqual([]);
  });

  it("maps interrupted (graceful BFF shutdown) to failure, same as failed/timed_out", () => {
    // Regression: interrupted was a terminal status with no case in the
    // reducer, so it fell through to `return []` and left the "working"
    // toast stuck at its Infinity duration forever.
    const s = createRunToastState();
    reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t8", agentId: "pa1" }, bgCtx);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t8", agentId: "pa1", status: "interrupted" }, bgCtx),
    ).toEqual([{ kind: "failure", key: "teams:t8", agentId: "pa1", source: "teams" }]);
  });

  it("ignores other teams events and malformed payloads", () => {
    const s = createRunToastState();
    expect(reduceTeamsRunEvent(s, "teams.activity.logged", { runId: "x" }, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", null, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { agentId: "pa1", status: "failed" }, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t7", status: 42 }, bgCtx)).toEqual([]);
  });
});

describe("reduceTeamsActivityEvent — approvals", () => {
  it("emits an attention action on approval.created when no teams panel is active", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.created", entityType: "approval", entityId: "ap1", agentId: "pa1" },
        bgCtx,
      ),
    ).toEqual([
      { kind: "attention", key: "approval:ap1", message: "An agent is awaiting your approval", approvalId: "ap1", agentId: "pa1" },
    ]);
  });

  it("names what is being approved when details.type is a known approval type", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.created", entityId: "ap1", details: { type: "hire_agent" } },
        bgCtx,
      ),
    ).toEqual([
      { kind: "attention", key: "approval:ap1", message: "An agent wants to hire a new agent", approvalId: "ap1" },
    ]);
  });

  it("falls back to the generic line for an unknown or prototype-shaped type", () => {
    for (const type of ["some_future_type", "constructor"]) {
      expect(
        reduceTeamsActivityEvent(
          "teams.activity.logged",
          { action: "approval.created", entityId: "ap1", details: { type } },
          bgCtx,
        ),
      ).toEqual([
        { kind: "attention", key: "approval:ap1", message: "An agent is awaiting your approval", approvalId: "ap1" },
      ]);
    }
  });

  it("emits an attention action on approval.resubmitted (ball back in the user's court)", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.resubmitted", entityId: "ap1" },
        bgCtx,
      ),
    ).toEqual([
      { kind: "attention", key: "approval:ap1", message: "An agent is awaiting your approval", approvalId: "ap1" },
    ]);
  });

  it("suppresses while a teams panel is active (the inbox band is on screen)", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.created", entityId: "ap1" },
        chatCtx([], true),
      ),
    ).toEqual([]);
  });

  it("dismisses on every action that ends the wait, even while suppressed", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.approved", entityId: "ap1" },
        chatCtx([], true),
      ),
    ).toEqual([{ kind: "dismiss", key: "approval:ap1" }]);
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.rejected", entityId: "ap2" },
        bgCtx,
      ),
    ).toEqual([{ kind: "dismiss", key: "approval:ap2" }]);
    // revision_requested hands the work back to the agent, so the sticky toast
    // must clear. It previously fell through to [] and outlived its approval.
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "approval.revision_requested", entityId: "ap3" },
        bgCtx,
      ),
    ).toEqual([{ kind: "dismiss", key: "approval:ap3" }]);
  });

  it("ignores non-approval actions, other events, and malformed payloads", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        { action: "issue.work_product_created", entityId: "i1" },
        bgCtx,
      ),
    ).toEqual([]);
    expect(
      reduceTeamsActivityEvent("teams.heartbeat.run.status", { action: "approval.created", entityId: "x" }, bgCtx),
    ).toEqual([]);
    expect(reduceTeamsActivityEvent("teams.activity.logged", null, bgCtx)).toEqual([]);
    expect(
      reduceTeamsActivityEvent("teams.activity.logged", { action: "approval.created" }, bgCtx),
    ).toEqual([]);
  });
});

describe("reduceTeamsActivityEvent — thread interactions (a paused task)", () => {
  const created = (details: Record<string, unknown>) => ({
    action: "issue.thread_interaction_created",
    entityType: "issue",
    entityId: "issue-1",
    details,
  });

  it("emits a kind-specific attention action pointing at the blocked task", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        created({
          interactionId: "int-1",
          interactionKind: "request_confirmation",
          addresseeAgentId: null,
        }),
        bgCtx,
      ),
    ).toEqual([
      {
        kind: "attention",
        key: "interaction:int-1",
        message: "A thread is paused waiting for your confirmation",
        issueId: "issue-1",
        interactionId: "int-1",
      },
    ]);
  });

  it("gives each interaction kind its own copy", () => {
    const copy = (kind: string) =>
      (
        reduceTeamsActivityEvent(
          "teams.activity.logged",
          created({ interactionId: "int-1", interactionKind: kind }),
          bgCtx,
        )[0] as { message: string }
      ).message;
    const kinds = [
      "request_confirmation",
      "request_checkbox_confirmation",
      "ask_user_questions",
      "suggest_tasks",
    ];
    const messages = kinds.map(copy);
    expect(new Set(messages).size).toBe(kinds.length);
    expect(copy("some_future_kind")).toBe("A thread is paused waiting for your input");
  });

  it("keys on the interaction, so two waits on one task do not replace each other", () => {
    const keyOf = (interactionId: string) =>
      (
        reduceTeamsActivityEvent(
          "teams.activity.logged",
          created({ interactionId, interactionKind: "request_confirmation" }),
          bgCtx,
        )[0] as { key: string }
      ).key;
    expect(keyOf("int-1")).not.toBe(keyOf("int-2"));
  });

  it("stays silent when the interaction is addressed to another agent", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        created({
          interactionId: "int-1",
          interactionKind: "request_confirmation",
          addresseeAgentId: "agent-9",
        }),
        bgCtx,
      ),
    ).toEqual([]);
  });

  it("suppresses while a teams panel is active", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        created({ interactionId: "int-1", interactionKind: "request_confirmation" }),
        chatCtx([], true),
      ),
    ).toEqual([]);
  });

  it("dismisses on accepted / rejected / expired, even while suppressed", () => {
    for (const action of [
      "issue.thread_interaction_accepted",
      "issue.thread_interaction_rejected",
      "issue.thread_interaction_expired",
    ]) {
      expect(
        reduceTeamsActivityEvent(
          "teams.activity.logged",
          { action, entityId: "issue-1", details: { interactionId: "int-1" } },
          chatCtx([], true),
        ),
      ).toEqual([{ kind: "dismiss", key: "interaction:int-1" }]);
    }
  });

  it("ignores an interaction event with no interactionId to key on", () => {
    expect(
      reduceTeamsActivityEvent(
        "teams.activity.logged",
        created({ interactionKind: "request_confirmation" }),
        bgCtx,
      ),
    ).toEqual([]);
  });
});

describe("reduceChatFrame — room runs", () => {
  const room = { runId: "r1", agentId: "pulse", roomId: "room-gtm" };
  const away: RunToastContext = { watchedChatKeys: new Set(), teamsPanelActive: false, visibleRoomId: null };

  it("ends with the reply's first line: the text after the last tool call", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1"), away, room);
    reduceChatFrame(s, { type: "tool_start", tool: "web_search", args: { query: "x" }, runId: "r1" }, away, room);
    reduceChatFrame(s, { type: "chunk", content: "Here", append: true, runId: "r1" }, away, room);
    reduceChatFrame(s, { type: "chunk", content: " it is\nmore", append: true, runId: "r1" }, away, room);
    expect(reduceChatFrame(s, runEnded("r1", "done"), away, room)).toEqual([
      { kind: "success", key: "r1", agentId: "pulse", source: "chat", roomId: "room-gtm", line: "Here it is" },
    ]);
  });

  it("never notifies a run in the room on screen, and dismisses one that ends there", () => {
    const s = createRunToastState();
    const onScreen: RunToastContext = { ...away, visibleRoomId: "room-gtm" };
    expect(reduceChatFrame(s, chunk("r1"), onScreen, room)).toEqual([]);
    expect(reduceChatFrame(s, runEnded("r1", "done"), onScreen, room)).toEqual([]);
    reduceChatFrame(s, chunk("r2"), away, { ...room, runId: "r2" });
    expect(reduceChatFrame(s, runEnded("r2", "done"), onScreen, { ...room, runId: "r2" })).toEqual([{ kind: "dismiss", key: "r2" }]);
  });

  it("a run in a side room on screen (an app's conversation) is quiet too, beside another main room", () => {
    const s = createRunToastState();
    const beside: RunToastContext = { ...away, visibleRoomId: "room-other", sideRoomIds: ["room-gtm"] };
    expect(reduceChatFrame(s, chunk("r1"), beside, room)).toEqual([]);
    expect(reduceChatFrame(s, runEnded("r1", "done"), beside, room)).toEqual([]);
  });
});

describe("reduceTeamsRunEvent — what a failed team run says", () => {
  it("carries the task and the error code", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = { watchedChatKeys: new Set(), teamsPanelActive: false };
    reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "a1" }, ctx);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status",
      { runId: "t1", agentId: "a1", status: "failed", issueId: "i1", errorCode: "process_lost", error: "lost" }, ctx)).toEqual([
      { kind: "failure", key: "teams:t1", agentId: "a1", source: "teams", issueId: "i1", errorCode: "process_lost", error: "lost" },
    ]);
  });
});
