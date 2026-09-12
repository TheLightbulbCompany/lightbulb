import { describe, expect, it, vi } from "vitest";
import { HEARTBEAT_MS, createRunCallbacks, createRunSupervisor, transformAgentEvent } from "./runs.js";

const ENV = {
  ISOL8_API_URL: "https://api.flickmeon.ai/",
  ISOL8_OWNER_ID: "owner_123",
  OPENCLAW_HOOK_TOKEN: "hook-token",
};
const ENDED = { runId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", type: "run.ended" as const, at: "2026-09-12T03:15:00.000Z", outcome: "completed" as const };

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
/** Two more accepts on the SAME room session — the host would serialize these turns. */
const RUN2 = { ...RUN, runId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd" };
const RUN3 = { ...RUN, runId: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" };
const ENTRY = { sessionId: "session-1", model: "claude-sonnet-4-6", inputTokens: 1200, outputTokens: 300, cacheRead: 5000, cacheWrite: 100, estimatedCostUsd: 0.0123 };
/** A turn that never settles, so the run it belongs to stays the session's running one. */
const pending = () => vi.fn(() => new Promise<"dispatched">(() => {}));

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

  it("keeps streaming after the host blanks the session key, and ignores another run's events", async () => {
    const h = harness();
    let finish!: (v: "dispatched") => void;
    h.runs.start(RUN, () => new Promise((resolve) => { finish = resolve; }));
    h.emit({ stream: "lifecycle", data: { phase: "start" } });
    // The host blanks sessionKey on every non-lifecycle stream for a surface the control
    // UI cannot see, and only attaches deliverySessionKey under a context claim.
    h.emit({ stream: "assistant", sessionKey: undefined, deliverySessionKey: undefined, data: { text: "Hi", delta: "Hi" } });
    h.emit({ runId: "other-sdk-run", stream: "assistant", sessionKey: undefined, deliverySessionKey: undefined, data: { text: "nope", delta: "nope" } });
    finish("dispatched");
    await h.settle();
    expect(h.types()).toEqual(["run_active", "chunk", "chunk", "done", "run_ended", "run_active"]);
    expect(h.frames.push.mock.calls[1][2].payload.frame).toMatchObject({ content: "Hi", append: true });
    expect(h.frames.push.mock.calls[2][2].payload.frame).toMatchObject({ content: "Hi" }); // the resync is not empty
    expect(JSON.stringify(h.frames.push.mock.calls)).not.toContain("nope");
  });

  it("ends with error after a lifecycle error and never re-runs", async () => {
    const h = harness();
    h.runs.start(RUN, async () => { h.emit({ stream: "lifecycle", data: { phase: "start" } }); h.emit({ stream: "lifecycle", data: { phase: "error", error: "boom", stopReason: "timeout" } }); return "dispatched"; });
    await h.settle();
    expect(h.types()).toEqual(["run_active", "error", "run_ended", "run_active"]);
    expect(h.sent().at(-1)).toMatchObject({ type: "run.ended", outcome: "error", errorKind: "timeout", usage: expect.any(Object) });
  });

  it("reports ignored without content frames or usage when the ambient gate declines", async () => {
    const h = harness();
    h.runs.start(RUN, async () => "ignored");
    await h.settle();
    // Acceptance is reported before the gate has ruled, so the activity indicator turns
    // on and back off — but nothing is rendered into the room and no usage is billed.
    expect(h.types()).toEqual(["run_active", "run_ended", "run_active"]);
    expect(h.sent()).toEqual([
      { runId: RUN.runId, type: "run.started", at: "2026-09-12T03:15:00.000Z", sessionKey: RUN.sessionKey },
      { runId: RUN.runId, type: "run.ended", at: "2026-09-12T03:15:00.000Z", outcome: "ignored" },
    ]);
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

  it("serializes a second turn on the same session behind the first", async () => {
    const h = harness();
    let finishFirst!: (v: "dispatched") => void;
    let finishSecond!: (v: "dispatched") => void;
    const second = vi.fn(() => new Promise<"dispatched">((resolve) => { finishSecond = resolve; }));
    h.runs.start(RUN, () => new Promise((resolve) => { finishFirst = resolve; }));
    h.runs.start(RUN2, second);
    expect(second).not.toHaveBeenCalled();
    // The reply stamp names the running turn, not the newest accepted one.
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN.runId);
    expect(h.runs.steer(RUN2.runId, "hurry")).toBe("not_found"); // nothing to steer yet
    finishFirst("dispatched");
    await h.settle();
    expect(second).toHaveBeenCalledOnce();
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN2.runId);
    // The shared session row is read once, by the run that actually turned.
    expect(h.sdk.getSessionEntry).toHaveBeenCalledOnce();
    finishSecond("dispatched");
    await h.settle();
    expect(h.sdk.getSessionEntry).toHaveBeenCalledTimes(2);
  });

  it("marks a queued run started at accept and heartbeats it while it waits", async () => {
    vi.useFakeTimers();
    const h = harness();
    h.runs.start(RUN, pending());
    h.runs.start(RUN2, pending());
    expect(h.sent().map((e: any) => [e.runId, e.type])).toEqual([[RUN.runId, "run.started"], [RUN2.runId, "run.started"]]);
    expect(h.types()).toEqual(["run_active", "run_active"]);
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS);
    expect(h.sent().filter((e: any) => e.type === "run.heartbeat").map((e: any) => e.runId).sort()).toEqual([RUN.runId, RUN2.runId]);
    vi.useRealTimers();
  });

  it("aborts a queued run without ever running it, and lets the one behind it through", async () => {
    const h = harness();
    let finishFirst!: (v: "dispatched") => void;
    const second = pending();
    const third = pending();
    h.runs.start(RUN, () => new Promise((resolve) => { finishFirst = resolve; }));
    h.runs.start(RUN2, second);
    h.runs.start(RUN3, third);
    expect(h.runs.abort(RUN2.runId)).toBe("aborting");
    await h.settle();
    expect(second).not.toHaveBeenCalled();
    expect(h.sdk.abortEmbeddedAgentRun).not.toHaveBeenCalled(); // the host never saw this turn
    // The session row belongs to whatever turn last ran on it, so a run that never
    // launched must report zeros rather than double-count another run's tokens.
    expect(h.sdk.getSessionEntry).not.toHaveBeenCalled();
    expect(h.sent().find((e: any) => e.runId === RUN2.runId && e.type === "run.ended")).toMatchObject({
      outcome: "aborted",
      usage: { model: "unknown", inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, estimatedCostUsd: null },
    });
    finishFirst("dispatched");
    await h.settle();
    expect(third).toHaveBeenCalledOnce();
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN3.runId);
  });

  it("attributes an overlapping event to the running run, not the queued one", async () => {
    const h = harness();
    let finishFirst!: (v: "dispatched") => void;
    h.runs.start(RUN, () => new Promise((resolve) => { finishFirst = resolve; }));
    h.emit({ stream: "lifecycle", data: { phase: "start" } });
    h.runs.start(RUN2, pending());
    h.emit({ stream: "assistant", sessionKey: undefined, deliverySessionKey: undefined, data: { text: "Hi", delta: "Hi" } });
    const chunks = h.frames.push.mock.calls.map((c: any[]) => c[2].payload.frame).filter((f: any) => f.type === "chunk");
    expect(chunks).toEqual([{ type: "chunk", content: "Hi", append: true, runId: RUN.runId, agentId: "friday" }]);
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN.runId);
    finishFirst("dispatched");
    await h.settle();
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN2.runId);
  });
});

describe("createRunCallbacks", () => {
  /** Drives the back-off sleeps without waiting for them, then awaits the send. */
  async function send(fetch: ReturnType<typeof vi.fn>, env = ENV, log?: (message: string) => void) {
    vi.useFakeTimers();
    try {
      const sent = createRunCallbacks({ fetch: fetch as any, env: () => env, log }).send(ENDED);
      await vi.runAllTimersAsync();
      await expect(sent).resolves.toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  }

  it("posts the event to the owner's run-events route with the hook token", async () => {
    const fetch = vi.fn(async () => ({ status: 200 }));
    await send(fetch);
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0] as any[];
    expect(url).toBe("https://api.flickmeon.ai/api/v1/internal/messaging/runs/owner_123/events");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "content-type": "application/json", Authorization: "Bearer hook-token" });
    expect(JSON.parse(init.body)).toEqual(ENDED);
  });

  it("retries a 5xx and a network failure, and treats any status below 500 as final", async () => {
    const flaky = vi.fn()
      .mockRejectedValueOnce(new Error("socket hang up"))
      .mockResolvedValueOnce({ status: 502 })
      .mockResolvedValueOnce({ status: 200 });
    await send(flaky);
    expect(flaky).toHaveBeenCalledTimes(3);

    const unknownRun = vi.fn(async () => ({ status: 404 }));
    await send(unknownRun);
    expect(unknownRun).toHaveBeenCalledOnce();
  });

  it("gives up after three attempts and reports a misconfigured container without rejecting", async () => {
    const down = vi.fn(async () => ({ status: 503 }));
    const logged: string[] = [];
    await send(down, ENV, (message) => logged.push(message));
    expect(down).toHaveBeenCalledTimes(3);

    const unsent = vi.fn();
    await send(unsent, {}, (message) => logged.push(message));
    expect(unsent).not.toHaveBeenCalled();
    expect(logged).toEqual([
      "lightbulb: run callback run.ended undelivered for bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      "lightbulb: run callback run.ended undelivered for bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ]);
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
