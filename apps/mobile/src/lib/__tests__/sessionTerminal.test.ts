import { ApiError, SNAPSHOT_WAIT_MS, type SessionScreen } from "@lightbulb/shared";

import { CLEAR, createScreenFeed, createTypist, STALE_AFTER_MS, withCtrl, type TerminalStep } from "../sessionTerminal";

const b64 = (text: string) => btoa(text);
const frame = (kind: SessionScreen["kind"], seq: number, text = `${kind}${seq}`, more: Partial<SessionScreen> = {}): SessionScreen =>
  ({ sessionId: "s", generation: "g", epoch: 1, seq, cols: 80, rows: 24, kind, data: b64(text), ...more });

function feed() {
  const steps: TerminalStep[] = [];
  const onLive = jest.fn();
  const onSeen = jest.fn();
  const it = createScreenFeed({ draw: (batch) => steps.push(...batch), onLive, onSeen });
  /** What the page would write, in order: "!" marks a reset, text is decoded, the CLEAR prefix reads as "<clear>". */
  const written = () => steps.map((s) => `${s.fresh ? "!" : ""}${s.data.map((d) => (d === CLEAR ? "<clear>" : atob(d))).join("")}`);
  return { it, steps, onLive, onSeen, written };
}

beforeEach(() => jest.useFakeTimers());
afterEach(() => jest.useRealTimers());

describe("createScreenFeed", () => {
  it("clears the screen before the bytes are 'ESC[?1049l ESC[r ESC[2J ESC[H'", () => {
    expect(atob(CLEAR)).toBe("\x1b[?1049l\x1b[r\x1b[2J\x1b[H");
  });

  it("writes history under the snapshot, then output, when they arrive in order", () => {
    const { it, steps, written, onLive, onSeen } = feed();
    it.receive(frame("history", 0));
    it.receive(frame("history", 1));
    expect(steps).toEqual([]);
    it.receive(frame("snapshot", 2));
    it.receive(frame("output", 3));
    expect(written()).toEqual(["!history0", "history1", "<clear>snapshot2", "output3"]);
    // The first frame starts the terminal over at the Mac's size; the snapshot puts a reader back.
    expect(steps[0]).toMatchObject({ fresh: true, resize: true, cols: 80, rows: 24, back: false });
    expect(steps[2]).toMatchObject({ fresh: false, resize: false, back: true });
    expect(steps[3]).toMatchObject({ fresh: false, back: false });
    expect(onLive).toHaveBeenLastCalledWith(true);
    expect(onSeen).toHaveBeenCalled();
    expect(it.epoch()).toBe(1);
  });

  it("starts the picture over, history first, when the burst arrives after its snapshot", () => {
    const { it, steps, written } = feed();
    it.receive(frame("snapshot", 2));
    it.receive(frame("output", 3));
    expect(written()).toEqual(["!<clear>snapshot2", "output3"]);
    it.receive(frame("history", 1));
    expect(written().slice(2)).toEqual(["!history1", "<clear>snapshot2", "output3"]);
    // The same relay run drawn again: the page keeps a reader on their line through it.
    expect(steps[2]).toMatchObject({ fresh: true, again: true });
    expect(steps[3]).toMatchObject({ fresh: false, back: true });
  });

  it("holds a snapshot that is ahead of a missing frame, and writes it in place when the frame comes", () => {
    const { it, written } = feed();
    it.receive(frame("snapshot", 0));
    it.receive(frame("snapshot", 2));
    expect(written()).toEqual(["!<clear>snapshot0"]);
    it.receive(frame("output", 1));
    expect(written()).toEqual(["!<clear>snapshot0", "output1", "<clear>snapshot2"]);
    // Nothing is left for the hold timer.
    jest.advanceTimersByTime(SNAPSHOT_WAIT_MS);
    expect(written()).toHaveLength(3);
  });

  it("writes a held snapshot as it is once the frames below it are late", () => {
    const { it, written } = feed();
    it.receive(frame("snapshot", 0));
    it.receive(frame("snapshot", 2));
    jest.advanceTimersByTime(SNAPSHOT_WAIT_MS - 1);
    expect(written()).toEqual(["!<clear>snapshot0"]);
    jest.advanceTimersByTime(1);
    expect(written()).toEqual(["!<clear>snapshot0", "<clear>snapshot2"]);
    // The late frame is below the picture now: dropped.
    it.receive(frame("output", 1));
    expect(written()).toHaveLength(2);
  });

  it("starts the terminal over for a new relay run and resizes when the Mac's grid changes", () => {
    const { it, steps } = feed();
    it.receive(frame("snapshot", 0));
    it.receive(frame("output", 1, "wide", { cols: 120, rows: 40 }));
    expect(steps[1]).toMatchObject({ fresh: false, resize: true, cols: 120, rows: 40 });
    it.receive(frame("snapshot", 0, "next run", { epoch: 2, cols: 120, rows: 40 }));
    expect(steps[2]).toMatchObject({ fresh: true, again: false, resize: false, back: true });
    expect(it.epoch()).toBe(2);
  });

  it("goes stale after 30 s with no frame; only a ping that names the next frame keeps it live", () => {
    const { it, onLive } = feed();
    it.receive(frame("snapshot", 0));
    jest.advanceTimersByTime(STALE_AFTER_MS - 1);
    it.receive(frame("ping", 1, ""));
    jest.advanceTimersByTime(STALE_AFTER_MS - 1);
    expect(onLive).not.toHaveBeenCalledWith(false);
    // One ahead of the frame this view waits for: a frame was lost, so it proves nothing.
    it.receive(frame("ping", 5, ""));
    jest.advanceTimersByTime(1);
    expect(onLive).toHaveBeenLastCalledWith(false);
  });

  it("stops its timers when disposed", () => {
    const { it, onLive, written } = feed();
    it.receive(frame("snapshot", 0));
    it.receive(frame("snapshot", 2));
    it.dispose();
    jest.advanceTimersByTime(STALE_AFTER_MS);
    expect(written()).toHaveLength(1);
    expect(onLive).not.toHaveBeenCalledWith(false);
  });
});

describe("createTypist", () => {
  const decode = (data: string) => atob(data);
  function typist(over: { canType?: () => boolean; send?: jest.Mock } = {}) {
    const send = over.send ?? jest.fn(async () => {});
    const onRefused = jest.fn();
    const it = createTypist({ send, epoch: () => 7, canType: over.canType ?? (() => true), onRefused });
    return { it, send, onRefused };
  }
  const settle = async () => { for (let i = 0; i < 100; i += 1) await Promise.resolve(); };

  it("sends raw keys, in order, with the epoch they were typed against", async () => {
    const { it, send } = typist();
    it.type("l"); it.type("s"); it.type("\r"); it.type("\x1b[A");
    await settle();
    expect(send.mock.calls.map(([data, epoch]) => [decode(data), epoch])).toEqual([["l", 7], ["s", 7], ["\r", 7], ["\x1b[A", 7]]);
  });

  it("never sends a mouse report or one of xterm's own probe replies", async () => {
    const { it, send } = typist();
    it.type("\x1b[<0;10;5M"); it.type("\x1b[?1;2c"); it.type("\x1b[<64;1;1Ma");
    await settle();
    expect(send.mock.calls.map(([data]) => decode(data))).toEqual(["a"]);
  });

  it("sends nothing before the screen has an epoch, and keeps a held Ctrl for later", async () => {
    let at = -1;
    const send = jest.fn(async (_data: string, _epoch: number) => {});
    const it = createTypist({ send, epoch: () => at, canType: () => true, onRefused: jest.fn() });
    it.setCtrl(true);
    expect(it.type("c")).toBe(false);
    await settle();
    expect(send).not.toHaveBeenCalled();
    at = 0;
    expect(it.type("c")).toBe(true);
    await settle();
    expect(send.mock.calls.map(([data, epoch]) => [decode(data), epoch])).toEqual([["\x03", 0]]);
  });

  it("sends nothing for someone who may not type", async () => {
    const { it, send } = typist({ canType: () => false });
    expect(it.type("rm -rf /\r")).toBe(false);
    await settle();
    expect(send).not.toHaveBeenCalled();
  });

  it("applies a sticky Ctrl to the next key only", async () => {
    const { it, send } = typist();
    it.setCtrl(true);
    expect(it.type("c")).toBe(true);
    expect(it.type("c")).toBe(false);
    await settle();
    expect(send.mock.calls.map(([data]) => decode(data))).toEqual(["\x03", "c"]);
    expect([withCtrl("a"), withCtrl("Z"), withCtrl("["), withCtrl("1"), withCtrl("ab")]).toEqual(["\x01", "\x1a", "\x1b", "1", "ab"]);
  });

  it("splits a long paste into calls of at most 4096 bytes", async () => {
    const { it, send } = typist();
    it.type("x".repeat(5000));
    await settle();
    expect(send.mock.calls.map(([data]) => decode(data).length)).toEqual([4096, 904]);
  });

  it("drops what is queued behind a refusal and says so; an offline Mac loses only that batch", async () => {
    let allowed = true;
    const send = jest.fn()
      .mockRejectedValueOnce(new ApiError(409, { code: "LOCAL_SESSION_OFFLINE" }))
      .mockRejectedValueOnce(new ApiError(403, null))
      .mockResolvedValue(undefined);
    const { it, onRefused } = typist({ send, canType: () => allowed });
    onRefused.mockImplementation(() => { allowed = false; });
    it.type("a"); it.type("b"); it.type("c");
    await settle();
    expect(send).toHaveBeenCalledTimes(2);
    expect(onRefused).toHaveBeenCalledTimes(1);
  });
});
