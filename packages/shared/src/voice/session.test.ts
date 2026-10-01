import { describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "../live/types";
import { ApiError } from "../api/error";
import { STILL_WAKING, VoiceCallSession, type VoiceCallView, type VoiceChannel } from "./session";

const dmStart = { callId: "call-1", sdp: "answer", deadline: 1_000_000, kind: "dm", voiceAgentId: "a", voiceName: "Finance", team: [{ id: "a", name: "Finance" }] };
const roomStart = { callId: "call-1", sdp: "answer", deadline: 1_000_000, kind: "room", voiceAgentId: "ceo", voiceName: "CEO", team: [{ id: "ceo", name: "CEO" }, { id: "fin", name: "Finance" }, { id: "gro", name: "Growth" }] };

function harness(start: Record<string, unknown> = dmStart, turns: Array<{ turnId: string; agentId: string }> = []) {
  let clock = 0;
  let step = 200;
  const timers: Array<() => void> = [];
  let turnN = 0;
  const sent: Array<Record<string, unknown>> = [];
  const peer = { offerSdp: "offer", channel: null as unknown as VoiceChannel, accept: async () => {}, close: vi.fn(), setMuted: vi.fn(), levels: () => ({ mic: 0, out: 0 }) };
  const channel: VoiceChannel = { send: (d) => sent.push(JSON.parse(d)), onmessage: null };
  peer.channel = channel;
  const views: VoiceCallView[] = [];
  const handlers = new Set<(e: LiveEvent) => void>();
  const api = {
    post: vi.fn(async (path: string) => {
      if (path === "/calls") return start;
      if (path.endsWith("/turns")) return turns[turnN++] ?? { turnId: `turn-${api.post.mock.calls.length}`, agentId: "a" };
      return { ended: true };
    }),
  };
  const session = new VoiceCallSession({
    api: api as never,
    rtc: { open: async () => peer },
    onEvent: (h) => { handlers.add(h); return () => handlers.delete(h); },
    now: () => (clock += step),
    setTimer: ((fn: () => void) => timers.push(fn)) as never,
    clearTimer: (() => {}) as never,
  });
  session.subscribe((v) => views.push(v as never));
  const oai = (event: Record<string, unknown>) => channel.onmessage?.({ data: JSON.stringify(event) });
  const frame = (turnId: string, f: Record<string, unknown>) =>
    handlers.forEach((h) => h({ schemaVersion: 1, eventId: "e", type: "call.turn.frame", occurredAt: "", payload: { callId: "call-1", turnId, agentId: "x", frame: f } } as never));
  return { session, api, sent, oai, frame, views, peer, timers, freezeClock: () => { step = 0; } };
}

describe("VoiceCallSession", () => {
  it("posts a turn with the latest utterance on delegation", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    h.oai({ type: "session.input_transcript.delta", delta: "check the stripe payout" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/turns", expect.objectContaining({ utterance: "check the stripe payout" })));
  });

  it("maps tool progress to thinking and answers to commentary", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
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
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
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
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    h.api.post.mockRejectedValueOnce(new Error("503"));
    h.oai({ type: "session.input_transcript.delta", delta: "hi" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", content: "Sorry, I couldn't reach the team for that." })));
  });

  it("retries a turn while the workspace wakes, with the same seq, saying so once", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    const waking = new ApiError(503, { detail: { code: "runtime_unavailable" } }, "503");
    h.api.post.mockRejectedValueOnce(waking).mockRejectedValueOnce(waking);
    h.oai({ type: "session.input_transcript.delta", delta: "check the board" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    for (const n of [2, 3]) {
      await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(n));
      await vi.waitFor(() => expect(h.timers.length).toBeGreaterThan(0));
      h.timers.pop()!();
    }
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(4));
    const seqs = h.api.post.mock.calls.filter((c) => String(c[0]).endsWith("/turns")).map((c) => ((c as unknown[])[1] as { seq: number }).seq);
    expect(seqs).toEqual([1, 1, 1]);
    expect(h.sent.filter((e) => e.content === STILL_WAKING)).toHaveLength(1);
    expect(h.sent.some((e) => e.type === "session.commentary.append")).toBe(false);
  });

  it("hangs up through the backend", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    await h.session.hangUp();
    expect(h.api.post).toHaveBeenLastCalledWith("/calls/call-1/end", {});
  });
  it("reports the backend's limit reason when the call can't start", async () => {
    const h = harness();
    h.api.post.mockRejectedValueOnce(new ApiError(402, { detail: { code: "voice_limit", reason: "free_minutes_used" } }, "402"));
    const views: string[] = [];
    h.session.subscribe((v) => views.push(`${v.state}:${v.error}`));
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    expect(views.at(-1)).toBe("failed:free_minutes_used");
  });

  async function delegated(h: ReturnType<typeof harness>) {
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
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
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    let release!: (v: never) => void; const go = (v: unknown) => release(v as never);
    h.api.post.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    h.oai({ type: "session.input_transcript.delta", delta: "q" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    h.frame("t-early", { type: "chunk", content: "Quick answer. ", append: true });
    h.frame("t-other", { type: "chunk", content: "Stray. ", append: true });
    h.frame("t-early", { type: "run_ended", outcome: "done" });
    expect(spoken(h)).toEqual([]);
    go({ turnId: "t-early" });
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
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    fns[0]();
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.instructions.append" }));
  });

  it("does not open a call when hung up while connecting", async () => {
    const h = harness();
    let release!: () => void;
    const peer = { offerSdp: "o", channel: { send: () => {}, onmessage: null }, accept: async () => {}, close: vi.fn() };
    const s = new VoiceCallSession({ api: h.api as never, rtc: { open: () => new Promise((r) => { release = () => r(peer); }) }, onEvent: () => () => {} });
    const started = s.start({ agentId: "a", agentName: "F" });
    await s.hangUp();
    release();
    await started;
    expect(h.api.post).not.toHaveBeenCalled();
    expect(peer.close).toHaveBeenCalled();
  });

  it("ends the backend call when accept fails after /calls", async () => {
    const h = harness();
    const s = new VoiceCallSession({ api: h.api as never, rtc: { open: async () => ({ offerSdp: "o", channel: { send: () => {}, onmessage: null }, accept: async () => { throw new Error("ice"); }, close: vi.fn() }) }, onEvent: () => () => {} });
    const views: string[] = [];
    s.subscribe((v) => views.push(v.state));
    await s.start({ agentId: "a", agentName: "F" });
    expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/end", {});
    expect(views.at(-1)).toBe("failed");
    expect(views).not.toContain("ended");
  });

  it("ends the call if hung up while /calls is in flight", async () => {
    const h = harness();
    let release!: (v: never) => void; const go = (v: unknown) => release(v as never);
    h.api.post.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    const started = h.session.start({ agentId: "a", agentName: "F" });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(1));
    await h.session.hangUp();
    go({ callId: "call-9", sdp: "x", deadline: 5, kind: "dm", voiceName: "F", team: [] });
    await started;
    expect(h.api.post).toHaveBeenCalledWith("/calls/call-9/end", {});
  });

  it("keeps the answer intact when the same tool starts twice", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "tool_start", tool: "terminal" });
    h.frame(t, { type: "chunk", content: "Looking now. Still looking. ", append: true });
    h.frame(t, { type: "tool_start", tool: "terminal" });
    h.frame(t, { type: "chunk", content: "It is done. ", append: true });
    h.frame(t, { type: "chunk", content: "It is done. Four." });
    h.frame(t, { type: "run_ended", outcome: "done" });
    expect(spoken(h)).toEqual(["Looking now. Still looking.", "It is done.", "Four."]);
  });

  it("orders deltas by seq and drops narration after its tool_start", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "chunk", content: "A. ", append: true, seq: 1 });
    h.frame(t, { type: "chunk", content: "C. ", append: true, seq: 3 });
    h.frame(t, { type: "chunk", content: "B. ", append: true, seq: 2 });
    h.frame(t, { type: "tool_start", tool: "x", seq: 5 });
    h.frame(t, { type: "chunk", content: "Late narration. ", append: true, seq: 4 });
    h.frame(t, { type: "run_ended", outcome: "done", seq: 6 });
    expect(spoken(h)).toEqual(["A.", "B.", "C."]);
  });

  it("ends the backend call on an unsolicited session.closed", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    h.oai({ type: "session.closed" });
    expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/end", {});
  });

  it("fails with connection_lost and ends the call when the peer drops", async () => {
    const h = harness();
    const peer = { offerSdp: "o", channel: { send: () => {}, onmessage: null }, accept: async () => {}, close: vi.fn(), onFailed: null as null | (() => void) };
    const s = new VoiceCallSession({ api: h.api as never, rtc: { open: async () => peer }, onEvent: () => () => {} });
    const views: string[] = [];
    s.subscribe((v) => views.push(`${v.state}:${v.error}`));
    await s.start({ agentId: "a", agentName: "F" });
    peer.onFailed!();
    expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/end", {});
    expect(views.at(-1)).toBe("failed:connection_lost");
  });

  it("repairs a swapped batch of five deltas", async () => {
    const h = harness();
    const t = await delegated(h);
    const batch = (from: number) => [0, 1, 2, 3, 4].forEach((i) => h.frame(t, { type: "chunk", content: `S${from + i}. `, append: true, seq: from + i }));
    batch(6);
    batch(1);
    h.frame(t, { type: "run_ended", outcome: "done", seq: 20 });
    expect(spoken(h)).toEqual(Array.from({ length: 10 }, (_, i) => `S${i + 1}.`));
  });

  it("does not speak a delta that lands after the closing chunk", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "chunk", content: "One. Two. ", seq: 5 });
    h.frame(t, { type: "chunk", content: "Two. ", append: true, seq: 4 });
    h.frame(t, { type: "run_ended", outcome: "done", seq: 6 });
    expect(spoken(h)).toEqual(["One. Two."]);
  });

  it("speaks the whole closing chunk when it arrives after run_ended", async () => {
    const h = harness();
    const t = await delegated(h);
    h.frame(t, { type: "run_ended", outcome: "done", seq: 6 });
    h.frame(t, { type: "chunk", content: "Final answer with no period", seq: 5 });
    expect(spoken(h)).toEqual(["Final answer with no period"]);
  });

  it("sends an increasing turn seq, even after a failed POST", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    const go = async (id: string, n: number) => {
      h.oai({ type: "session.input_transcript.delta", delta: "q" });
      h.oai({ type: "session.delegation.created", delegation: { id, target: "client" } });
      await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(1 + n));
    };
    await go("i1", 1);
    h.api.post.mockRejectedValueOnce(new Error("503"));
    await go("i2", 2);
    await go("i3", 3);
    const seqs = h.api.post.mock.calls.filter((c) => String(c[0]).endsWith("/turns")).map((c) => ((c as unknown[])[1] as { seq: number }).seq);
    expect(seqs).toEqual([1, 2, 3]);
  });

  it("stays quiet on a turn_conflict for the current turn", async () => {
    const h = harness();
    await h.session.start({ agentId: "agent-1", agentName: "Finance" });
    h.api.post.mockRejectedValueOnce(new ApiError(409, { detail: { code: "turn_conflict" } }, "409"));
    h.oai({ type: "session.input_transcript.delta", delta: "hi" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    await Promise.resolve();
    expect(h.sent.some((e) => e.type === "session.commentary.append")).toBe(false);
  });

  it("routes frames of parallel turns by turnId", async () => {
    const h = harness(roomStart, [{ turnId: "t-fin", agentId: "fin" }, { turnId: "t-gro", agentId: "gro" }]);
    await h.session.start({ roomId: "r" });
    h.oai({ type: "session.input_transcript.delta", delta: "check payouts" });
    h.oai({ type: "session.delegation.created", delegation: { id: "d1", target: "client" } });
    h.oai({ type: "session.input_transcript.delta", delta: " and draft a post" });
    h.oai({ type: "session.delegation.created", delegation: { id: "d2", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(3));
    h.frame("t-fin", { type: "tool_start", tool: "browser" });
    h.frame("t-gro", { type: "chunk", content: "Draft is ready. ", append: true });
    h.frame("t-gro", { type: "run_ended", outcome: "done" });
    // frames that beat a turn's POST are buffered and replayed once it resolves
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.thinking.append", delegation_id: "d1", content: "Finance is working: browser. Not done yet." })));
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", delegation_id: "d2", content: "Draft is ready." })));
    expect(h.views.at(-1)!.working).toEqual([{ agentId: "fin", label: "browser" }]);
  });

  it("supersedes only the same agent's older turn", async () => {
    const h = harness(roomStart, [{ turnId: "t-fin-1", agentId: "fin" }, { turnId: "t-fin-2", agentId: "fin" }]);
    await h.session.start({ roomId: "r" });
    h.oai({ type: "session.input_transcript.delta", delta: "check payouts" });
    h.oai({ type: "session.delegation.created", delegation: { id: "d1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    h.oai({ type: "session.input_transcript.delta", delta: " no, last month" });
    h.oai({ type: "session.delegation.created", delegation: { id: "d2", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(3));
    h.frame("t-fin-1", { type: "chunk", content: "Old answer. ", append: true });
    h.frame("t-fin-2", { type: "chunk", content: "Last month landed. ", append: true });
    h.frame("t-fin-2", { type: "run_ended", outcome: "done" }); // deltas sit in the reorder window until a closing frame
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ delegation_id: "d2", content: "Last month landed." })));
    expect(h.sent.some((e) => e.content === "Old answer.")).toBe(false);
  });

  it("posts a room target and exposes team + voice name", async () => {
    const h = harness(roomStart, []);
    await h.session.start({ roomId: "r" });
    expect(h.api.post).toHaveBeenCalledWith("/calls", expect.objectContaining({ roomId: "r", sdp: "offer" }));
    expect(h.views.at(-1)).toMatchObject({ kind: "room", voiceAgentId: "ceo", voiceName: "CEO", team: expect.arrayContaining([{ id: "fin", name: "Finance" }]) });
  });

  it("passes bands through to the peer, and reads zeros without one", async () => {
    const h = harness();
    expect(h.session.bands(3)).toEqual({ mic: [0, 0, 0], out: [0, 0, 0] });
    Object.assign(h.peer, { bands: (n: number) => ({ mic: new Array(n).fill(0.25), out: new Array(n).fill(0.5) }) });
    await h.session.start({ agentId: "a", agentName: "Finance" });
    expect(h.session.bands(2)).toEqual({ mic: [0.25, 0.25], out: [0.5, 0.5] });
  });

  it("keeps the last two caption lines", async () => {
    const h = harness(dmStart, []);
    await h.session.start({ agentId: "a", agentName: "Eng" });
    h.oai({ type: "session.input_transcript.delta", delta: "hi" });
    h.oai({ type: "session.output_transcript.delta", delta: "Hello!" });
    h.oai({ type: "session.input_transcript.delta", delta: "check payouts" });
    await vi.waitFor(() => expect(h.views.at(-1)!.captions).toEqual([
      { role: "agent", text: "Hello!" }, { role: "you", text: "check payouts" }]));
  });

  it("mutes the mic and tells the model", async () => {
    const h = harness(dmStart, []); await h.session.start({ agentId: "a", agentName: "Eng" });
    h.session.setMuted(true);
    expect(h.peer.setMuted).toHaveBeenCalledWith(true);
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.input_audio.mute" }));
    expect(h.views.at(-1)!.muted).toBe(true);
  });

  it("emits the latest captions from the trailing timer when throttled", async () => {
    const h = harness(dmStart, []);
    await h.session.start({ agentId: "a", agentName: "Eng" });
    h.oai({ type: "session.input_transcript.delta", delta: "hi" }); // leading edge
    h.freezeClock();
    const before = h.timers.length;
    h.oai({ type: "session.output_transcript.delta", delta: "Hello" });
    h.oai({ type: "session.output_transcript.delta", delta: "!" });
    expect(h.timers.length).toBe(before + 1); // one trailing timer, not one per delta
    expect(h.views.at(-1)!.captions).toEqual([{ role: "you", text: "hi" }]);
    h.timers.at(-1)!();
    expect(h.views.at(-1)!.captions).toEqual([{ role: "you", text: "hi" }, { role: "agent", text: "Hello!" }]);
  });

  it("unmutes the mic and tells the model", async () => {
    const h = harness(dmStart, []); await h.session.start({ agentId: "a", agentName: "Eng" });
    h.session.setMuted(true);
    h.session.setMuted(false);
    expect(h.peer.setMuted).toHaveBeenLastCalledWith(false);
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.input_audio.unmute" }));
    expect(h.views.at(-1)!.muted).toBe(false);
  });

  it("keeps the newer turn when POSTs for one agent resolve in reverse order", async () => {
    const h = harness(roomStart, []);
    await h.session.start({ roomId: "r" });
    const release: Array<(v: never) => void> = [];
    for (let i = 0; i < 2; i++) h.api.post.mockImplementationOnce(() => new Promise((r) => { release.push(r); }));
    h.oai({ type: "session.input_transcript.delta", delta: "check payouts" });
    h.oai({ type: "session.delegation.created", delegation: { id: "d1", target: "client" } });
    h.oai({ type: "session.input_transcript.delta", delta: " no, last month" });
    h.oai({ type: "session.delegation.created", delegation: { id: "d2", target: "client" } });
    await vi.waitFor(() => expect(release).toHaveLength(2));
    release[1]({ turnId: "t-new", agentId: "fin" } as never); // newer resolves first
    await Promise.resolve();
    release[0]({ turnId: "t-old", agentId: "fin" } as never);
    await vi.waitFor(() => expect(h.views.at(-1)!.pending).toBe(1)); // old turn dropped, new one awaits a frame
    h.frame("t-old", { type: "chunk", content: "Old answer. ", append: true });
    h.frame("t-new", { type: "chunk", content: "Last month landed. ", append: true });
    h.frame("t-new", { type: "run_ended", outcome: "done" });
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ delegation_id: "d2", content: "Last month landed." })));
    expect(h.sent.some((e) => e.content === "Old answer.")).toBe(false);
  });

  it("does not touch the view when a turn POST settles after hang-up", async () => {
    const h = harness(dmStart, []);
    await h.session.start({ agentId: "a", agentName: "Eng" });
    let release!: (v: never) => void;
    h.api.post.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    h.oai({ type: "session.delegation.created", delegation: { id: "d1", target: "client" } });
    await h.session.hangUp();
    const n = h.views.length;
    release({ turnId: "t1", agentId: "a" } as never);
    await Promise.resolve(); await Promise.resolve();
    expect(h.views.length).toBe(n);
  });
});
