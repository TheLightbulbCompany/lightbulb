import { describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "../live/types";
import { ApiError } from "../api/error";
import { VoiceCallSession, type VoiceChannel } from "./session";

function harness() {
  const sent: Array<Record<string, unknown>> = [];
  const channel: VoiceChannel = { send: (d) => sent.push(JSON.parse(d)), onmessage: null };
  const handlers = new Set<(e: LiveEvent) => void>();
  const api = {
    post: vi.fn(async (path: string) => {
      if (path === "/calls") return { callId: "call-1", sdp: "answer", deadline: 1_000_000 };
      if (path.endsWith("/turns")) return { turnId: `turn-${api.post.mock.calls.length}` };
      return { ended: true };
    }),
  };
  const session = new VoiceCallSession({
    api: api as never,
    rtc: { open: async () => ({ offerSdp: "offer", channel, accept: async () => {}, close: vi.fn() }) },
    onEvent: (h) => { handlers.add(h); return () => handlers.delete(h); },
    agentName: "Finance",
    now: () => 0,
    setTimer: (() => 0) as never,
    clearTimer: (() => {}) as never,
  });
  const oai = (event: Record<string, unknown>) => channel.onmessage?.({ data: JSON.stringify(event) });
  const frame = (turnId: string, f: Record<string, unknown>) =>
    handlers.forEach((h) => h({ schemaVersion: 1, eventId: "e", type: "call.turn.frame", occurredAt: "", payload: { callId: "call-1", turnId, agentId: "a", frame: f } } as never));
  return { session, api, sent, oai, frame };
}

describe("VoiceCallSession", () => {
  it("posts a turn with the latest utterance on delegation", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "check the stripe payout" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/turns", expect.objectContaining({ utterance: "check the stripe payout" })));
  });

  it("maps tool progress to thinking and answers to commentary", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "payout?" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    const turnId = (await h.api.post.mock.results[1].value).turnId;
    h.frame(turnId, { type: "tool_start", tool: "browser" });
    h.frame(turnId, { type: "chunk", content: "It landed this morning. ", append: true });
    h.frame(turnId, { type: "chunk", content: "$4,210", append: true });
    h.frame(turnId, { type: "run_ended", outcome: "done" });
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.thinking.append", delegation_id: "item_1", content: "Finance is working: browser. Not done yet." }));
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", delegation_id: "item_1", content: "It landed this morning." }));
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", delegation_id: "item_1", content: "$4,210" }));
  });

  it("drops frames for a superseded turn", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "first" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    const oldTurn = (await h.api.post.mock.results[1].value).turnId;
    h.oai({ type: "session.input_transcript.delta", delta: " no, second" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_2", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(3));
    h.frame(oldTurn, { type: "chunk", content: "Old answer. ", append: true });
    expect(h.sent.some((e) => e.content === "Old answer.")).toBe(false);
  });

  it("says it could not reach the agent when the turn fails", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.api.post.mockRejectedValueOnce(new Error("503"));
    h.oai({ type: "session.input_transcript.delta", delta: "hi" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", content: "Sorry, I couldn't reach Finance for that." })));
  });

  it("hangs up through the backend", async () => {
    const h = harness();
    await h.session.start("agent-1");
    await h.session.hangUp();
    expect(h.api.post).toHaveBeenLastCalledWith("/calls/call-1/end", {});
  });
  it("reports the backend's limit reason when the call can't start", async () => {
    const h = harness();
    h.api.post.mockRejectedValueOnce(new ApiError(402, { detail: { code: "voice_limit", reason: "free_minutes_used" } }, "402"));
    const views: string[] = [];
    h.session.subscribe((v) => views.push(`${v.state}:${v.error}`));
    await h.session.start("agent-1");
    expect(views.at(-1)).toBe("failed:free_minutes_used");
  });

  async function delegated(h: ReturnType<typeof harness>) {
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "q" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    return (await h.api.post.mock.results[1].value).turnId as string;
  }
  const spoken = (h: ReturnType<typeof harness>) => h.sent.filter((e) => e.type === "session.commentary.append").map((e) => e.content);

  it("speaks the closing answer once and intact after narration and a tool", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "chunk", content: "Let me check. ", append: true });
    h.frame(t, { type: "tool_start", tool: "browser" });
    h.frame(t, { type: "chunk", content: "It landed this morning. ", append: true });
    h.frame(t, { type: "chunk", content: "It landed this morning. $4,210" });
    h.frame(t, { type: "run_ended", outcome: "done" });
    expect(spoken(h)).toEqual(["Let me check.", "It landed this morning.", "$4,210"]);
  });

  it("speaks the whole closing chunk when no deltas arrived", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "chunk", content: "All good. Done" });
    h.frame(t, { type: "run_ended", outcome: "done" });
    expect(spoken(h)).toEqual(["All good.", "Done"]);
  });

  it("speaks nothing new when the closing chunk disagrees with what was spoken", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "chunk", content: "First bit. ", append: true });
    h.frame(t, { type: "chunk", content: "Totally different answer." });
    h.frame(t, { type: "run_ended", outcome: "done" });
    expect(spoken(h)).toEqual(["First bit."]);
  });

  it("replays frames that arrive before the turn id is known", async () => {
    const h = harness();
    await h.session.start("agent-1");
    let release!: (v: unknown) => void;
    h.api.post.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    h.oai({ type: "session.input_transcript.delta", delta: "q" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    h.frame("t-early", { type: "chunk", content: "Quick answer. ", append: true });
    h.frame("t-other", { type: "chunk", content: "Stray. ", append: true });
    h.frame("t-early", { type: "run_ended", outcome: "done" });
    expect(spoken(h)).toEqual([]);
    release({ turnId: "t-early" });
    await vi.waitFor(() => expect(spoken(h)).toEqual(["Quick answer."]));
  });

  it("de-duplicates the working label", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "tool_start", tool: "browser" });
    h.frame(t, { type: "tool_start", tool: "browser" });
    expect(h.sent.filter((e) => e.type === "session.thinking.append")).toHaveLength(1);
  });

  it("does not add a no-result note after an error apology", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "error", message: "boom" });
    h.frame(t, { type: "run_ended", outcome: "error" });
    expect(h.sent.some((e) => e.type === "session.thinking.append")).toBe(false);
  });

  it("warns one minute before the deadline", async () => {
    const h = harness();
    const fns: Array<() => void> = [];
    (h.session as never as { deps: { setTimer: unknown } }).deps.setTimer = (fn: () => void) => { fns.push(fn); return 0; };
    // deadline 1_000_000s => far in the future, warning is scheduled
    await h.session.start("agent-1");
    fns[0]();
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.instructions.append" }));
  });

  it("does not open a call when hung up while connecting", async () => {
    const h = harness();
    let release!: () => void;
    const peer = { offerSdp: "o", channel: { send: () => {}, onmessage: null }, accept: async () => {}, close: vi.fn() };
    const s = new VoiceCallSession({ api: h.api as never, rtc: { open: () => new Promise((r) => { release = () => r(peer); }) }, onEvent: () => () => {}, agentName: "F" });
    const started = s.start("a");
    await s.hangUp();
    release();
    await started;
    expect(h.api.post).not.toHaveBeenCalled();
    expect(peer.close).toHaveBeenCalled();
  });

  it("ends the backend call when accept fails after /calls", async () => {
    const h = harness();
    const s = new VoiceCallSession({ api: h.api as never, rtc: { open: async () => ({ offerSdp: "o", channel: { send: () => {}, onmessage: null }, accept: async () => { throw new Error("ice"); }, close: vi.fn() }) }, onEvent: () => () => {}, agentName: "F" });
    const views: string[] = [];
    s.subscribe((v) => views.push(v.state));
    await s.start("a");
    expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/end", {});
    expect(views.at(-1)).toBe("failed");
    expect(views).not.toContain("ended");
  });

  it("ends the call if hung up while /calls is in flight", async () => {
    const h = harness();
    let release!: (v: unknown) => void;
    h.api.post.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    const started = h.session.start("a");
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(1));
    await h.session.hangUp();
    release({ callId: "call-9", sdp: "x", deadline: 5 });
    await started;
    expect(h.api.post).toHaveBeenCalledWith("/calls/call-9/end", {});
  });
});
