import { describe, it, expect } from "vitest";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import {
  createRunToastState,
  reduceChatFrame,
  reduceTeamsRunEvent,
  MAX_TRACKED_RUNS,
  type RunToastContext,
} from "../runToastReducer";

const bgCtx: RunToastContext = {
  selectedAgentId: "agent-sel",
  chatViewActive: true,
  teamsPanelActive: false,
  selectedSessionId: null,
};

function chunk(runId: string, agentId?: string, sessionId?: string): ChatIncomingMessage {
  return { type: "chunk", content: "hi", runId, agent_id: agentId, session_id: sessionId };
}

function runEnded(
  runId: string,
  outcome: "done" | "error" | "aborted",
  agentId?: string,
  sessionId?: string,
): ChatIncomingMessage {
  return { type: "run_ended", runId, outcome, agent_id: agentId, session_id: sessionId };
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
    const ctx: RunToastContext = { ...bgCtx, chatViewActive: false };
    expect(reduceChatFrame(s, chunk("r1", "agent-sel"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("suppresses the watched agent when the run's session matches the active session", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = { ...bgCtx, selectedSessionId: "s1" };
    const watched: ChatIncomingMessage = chunk("r1", "agent-sel", "s1");
    expect(reduceChatFrame(s, watched, ctx)).toEqual([]);
    // terminal while STILL matching agent+session: stays silent
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel", "s1"), ctx)).toEqual([]);
  });

  it("suppresses the watched agent when both are on General (null session)", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = { ...bgCtx, selectedSessionId: null };
    const watched: ChatIncomingMessage = chunk("r1", "agent-sel"); // no session_id -> null
    expect(reduceChatFrame(s, watched, ctx)).toEqual([]);
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel"), ctx)).toEqual([]);
  });

  it("toasts a background run on a DIFFERENT session of the watched agent", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = { ...bgCtx, selectedSessionId: "s1" };
    expect(reduceChatFrame(s, chunk("r1", "agent-sel", "s2"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel", "s2"), ctx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("toasts a background run in a named session while the watched agent view is on General", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = { ...bgCtx, selectedSessionId: null };
    expect(reduceChatFrame(s, chunk("r1", "agent-sel", "s2"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("ignores frames without runId or agent_id, btw_result, and passive frames", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, chunk("r1", undefined), bgCtx)).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "btw_result", question: "q", text: "t" }, bgCtx),
    ).toEqual([]);
    expect(reduceChatFrame(s, { type: "heartbeat" }, bgCtx)).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "run_active", active: true, agent_id: "a" }, bgCtx),
    ).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "done", runId: "r1", agent_id: "agent-bg" }, bgCtx),
    ).toEqual([]);
  });
});

describe("reduceChatFrame — terminals (run_ended only)", () => {
  it("flips a shown run to success on outcome done", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "done"), bgCtx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("flips a shown run to failure on outcome error", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "error"), bgCtx)).toEqual([
      { kind: "failure", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("dismisses a shown run on outcome aborted", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "aborted"), bgCtx)).toEqual([
      { kind: "dismiss", key: "r1" },
    ]);
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
    const awayCtx: RunToastContext = { ...bgCtx, chatViewActive: false };
    expect(reduceChatFrame(s, runEnded("r1", "done"), awayCtx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("ignores a never-seen terminal with no agent_id", () => {
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
    const onTeams: RunToastContext = { ...bgCtx, teamsPanelActive: true };
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

  it("ignores other teams events and malformed payloads", () => {
    const s = createRunToastState();
    expect(reduceTeamsRunEvent(s, "teams.activity.logged", { runId: "x" }, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", null, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { agentId: "pa1", status: "failed" }, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t7", status: 42 }, bgCtx)).toEqual([]);
  });
});
