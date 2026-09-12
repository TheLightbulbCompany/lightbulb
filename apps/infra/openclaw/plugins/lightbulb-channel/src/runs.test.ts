import { describe, expect, it, vi } from "vitest";
import { HEARTBEAT_MS, createRunSupervisor, transformAgentEvent } from "./runs.js";

const RUN = {
  runId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  roomId: "22222222-2222-4222-8222-222222222222",
  agentId: "friday",
  sessionKey: "agent:friday:lightbulb:channel:22222222-2222-4222-8222-222222222222",
  frameChannel: "/ws/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/rooms/22222222-2222-4222-8222-222222222222/cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  frameEndpoint: "https://abc123.appsync-api.us-east-1.amazonaws.com/event",
  threadRootMessageId: null,
  eventKind: "user_request" as const,
};
const ENTRY = { sessionId: "session-1", model: "claude-sonnet-4-6", inputTokens: 1200, outputTokens: 300, cacheRead: 5000, cacheWrite: 100, estimatedCostUsd: 0.0123 };

function harness() {
  let listener: ((evt: any) => void) | undefined;
  const sdk = {
    onAgentEvent: vi.fn((fn: (evt: any) => void) => { listener = fn; return () => { listener = undefined; }; }),
    resolveActiveEmbeddedRunSessionId: vi.fn(() => "session-1"),
    abortEmbeddedAgentRun: vi.fn(() => true),
    queueAgentHarnessMessage: vi.fn(() => true),
    getSessionEntry: vi.fn(() => ENTRY),
  };
  const callbacks = { send: vi.fn(async () => {}) };
  const frames = { push: vi.fn(), flush: vi.fn(async () => {}) };
  const runs = createRunSupervisor({ sdk, callbacks, frames, uuid: () => "e1", now: () => new Date("2026-09-12T03:15:00Z") });
  const emit = (evt: any) => listener?.({ runId: "sdk-run", seq: 1, ts: 0, sessionKey: RUN.sessionKey, ...evt });
  const types = () => frames.push.mock.calls.map((c: any[]) => c[2].payload.frame.type);
  const sent = () => callbacks.send.mock.calls.map((c: any[]) => c[0]);
  const settle = () => new Promise((r) => setImmediate(r));
  return { sdk, callbacks, frames, runs, emit, types, sent, settle };
}

describe("run supervisor", () => {
  it("reports started on the first lifecycle event, streams frames, ends completed with usage and a full-text resync", async () => {
    const h = harness();
    let finish!: (v: "dispatched") => void;
    h.runs.start(RUN, () => new Promise((resolve) => { finish = resolve; }));
    h.emit({ stream: "lifecycle", data: { phase: "start" } });
    h.emit({ stream: "assistant", data: { text: "Hel", delta: "Hel" } });
    h.emit({ stream: "assistant", data: { text: "Hello", delta: "lo" } });
    h.emit({ stream: "tool", data: { phase: "start", name: "exec", toolCallId: "t1" } });
    finish("dispatched");
    await h.settle();
    expect(h.sent()[0]).toMatchObject({ runId: RUN.runId, type: "run.started", sessionKey: RUN.sessionKey });
    expect(h.types()).toEqual(["run_active", "chunk", "chunk", "tool_start", "chunk", "done", "run_ended", "run_active"]);
    const resync = h.frames.push.mock.calls[4][2].payload.frame;
    expect(resync).toMatchObject({ content: "Hello", runId: RUN.runId, agentId: "friday" });
    expect(resync.append).toBeUndefined();
    expect(h.sdk.getSessionEntry).toHaveBeenCalledWith({ agentId: "friday", sessionKey: RUN.sessionKey });
    expect(h.sent().at(-1)).toEqual({
      runId: RUN.runId, type: "run.ended", at: "2026-09-12T03:15:00.000Z", outcome: "completed",
      usage: { model: "claude-sonnet-4-6", inputTokens: 1200, outputTokens: 300, cacheReadTokens: 5000, cacheWriteTokens: 100, estimatedCostUsd: 0.0123 },
    });
    expect(h.frames.flush).toHaveBeenCalledWith(RUN.frameChannel);
  });

  it("ends with error after a lifecycle error and never re-runs", async () => {
    const h = harness();
    h.runs.start(RUN, async () => { h.emit({ stream: "lifecycle", data: { phase: "start" } }); h.emit({ stream: "lifecycle", data: { phase: "error", error: "boom", stopReason: "timeout" } }); return "dispatched"; });
    await h.settle();
    expect(h.types()).toEqual(["run_active", "error", "run_ended", "run_active"]);
    expect(h.sent().at(-1)).toMatchObject({ type: "run.ended", outcome: "error", errorKind: "timeout", usage: expect.any(Object) });
  });

  it("reports ignored without frames or usage when the ambient gate declines", async () => {
    const h = harness();
    h.runs.start(RUN, async () => "ignored");
    await h.settle();
    expect(h.types()).toEqual([]);
    expect(h.sent()).toEqual([{ runId: RUN.runId, type: "run.ended", at: "2026-09-12T03:15:00.000Z", outcome: "ignored" }]);
  });

  it("heartbeats every 30 s while running and stops at the end", async () => {
    vi.useFakeTimers();
    const h = harness();
    let finish!: (v: "dispatched") => void;
    h.runs.start(RUN, () => new Promise((resolve) => { finish = resolve; }));
    h.emit({ stream: "lifecycle", data: { phase: "start" } });
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2);
    expect(h.sent().filter((e: any) => e.type === "run.heartbeat")).toHaveLength(2);
    finish("dispatched");
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2);
    expect(h.sent().filter((e: any) => e.type === "run.heartbeat")).toHaveLength(2);
    vi.useRealTimers();
  });

  it("aborts and steers through the SDK by session, and answers not_found otherwise", () => {
    const h = harness();
    expect(h.runs.abort(RUN.runId)).toBe("not_found");
    h.runs.start(RUN, () => new Promise(() => {}));
    expect(h.runs.abort(RUN.runId)).toBe("aborting");
    expect(h.sdk.resolveActiveEmbeddedRunSessionId).toHaveBeenCalledWith(RUN.sessionKey);
    expect(h.sdk.abortEmbeddedAgentRun).toHaveBeenCalledWith("session-1");
    expect(h.runs.steer(RUN.runId, "focus on tests")).toBe("accepted");
    expect(h.sdk.queueAgentHarnessMessage).toHaveBeenCalledWith("session-1", "focus on tests");
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN.runId);
  });
});

describe("transformAgentEvent", () => {
  it("mirrors the backend mapping", () => {
    expect(transformAgentEvent("assistant", { text: "ab", delta: "b" })).toEqual({ type: "chunk", content: "b", append: true });
    expect(transformAgentEvent("assistant", { text: "ab", replace: true })).toEqual({ type: "chunk", content: "ab" });
    expect(transformAgentEvent("thinking", { text: "" })).toBeNull();
    expect(transformAgentEvent("tool", { phase: "update", toolCallId: "t", partialResult: "x" })).toEqual({ type: "tool_progress", toolCallId: "t", partialResult: "x" });
    expect(transformAgentEvent("tool", { phase: "result", name: "exec", toolCallId: "t", isError: true, result: "r" })).toEqual({ type: "tool_error", tool: "exec", toolCallId: "t", result: "r" });
    expect(transformAgentEvent("command_output", { output: "o" })).toBeNull();
    expect(transformAgentEvent("compaction", { phase: "start" })).toEqual({ type: "agent_activity", activity: "compaction", title: "start", detail: { phase: "start" } });
    expect(transformAgentEvent("plan", { title: "Plan" })).toEqual({ type: "agent_activity", activity: "plan", title: "Plan", detail: { title: "Plan" } });
    expect(transformAgentEvent("approval", { phase: "requested" })).toBeNull();
  });
});
