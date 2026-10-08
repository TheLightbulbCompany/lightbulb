import { expect, it, test } from "vitest";
import { NEW_SCREEN, receiveScreen, type ScreenOrder } from "./sessionScreen";
import { LIVE_EVENT_TYPES, parseLiveEvent, type SessionScreen } from "./types";

test("a screen frame carries one session's terminal bytes and nothing else", () => {
  const event = { schemaVersion: 1, eventId: "scr-1", type: "workspace.session.screen", occurredAt: "2026-10-03T00:00:00Z",
    payload: { sessionId: "s1", generation: "g1", epoch: 1760000000000, seq: 0, cols: 120, rows: 40, kind: "snapshot", data: "G1tI" } };
  expect(LIVE_EVENT_TYPES.has("workspace.session.screen")).toBe(true);
  expect(parseLiveEvent(event)).toEqual(event);
  for (const good of [{ kind: "history" }, { kind: "ping", data: "" }]) {
    expect(parseLiveEvent({ ...event, payload: { ...event.payload, ...good } })).not.toBeNull();
  }
  for (const bad of [{ kind: "input" }, { seq: -1 }, { cols: 0 }, { rows: 501 }, { data: 7 }, { epoch: 1.5 }, { kind: "ping" }]) {
    expect(parseLiveEvent({ ...event, payload: { ...event.payload, ...bad } })).toBeNull();
  }
});

const f = (seq: number, kind: SessionScreen["kind"] = "output", epoch = 1): SessionScreen =>
  ({ sessionId: "s", generation: "g", epoch, seq, cols: 80, rows: 24, kind, data: "" });
function feed(frames: SessionScreen[], order: ScreenOrder = NEW_SCREEN) {
  const written: string[] = [];
  for (const frame of frames) { const step = receiveScreen(order, frame); order = step.order; written.push(...step.write.map((w) => `${w.kind[0]}${w.seq}`)); }
  return { order, written };
}

it("writes nothing before a snapshot, then everything in seq order", () => {
  expect(feed([f(5), f(3, "snapshot"), f(4)]).written).toEqual(["s3", "o4", "o5"]);
});
it("drops duplicates and frames from an older relay run", () => {
  expect(feed([f(0, "snapshot", 2), f(1, "output", 2), f(1, "output", 2), f(7, "output", 1)]).written).toEqual(["s0", "o1"]);
});
it("stops writing after a gap it cannot fill, until the next snapshot, and takes no second history for a run it has drawn", () => {
  const gap = [f(0, "history"), f(1, "snapshot"), ...Array.from({ length: 17 }, (_, i) => f(i + 3))];
  const { order, written } = feed(gap);
  expect(written).toEqual(["h0", "s1"]);
  expect(feed([f(29, "history"), f(30, "snapshot"), f(31)], order).written).toEqual(["s30", "o31"]);
});
it("a new viewer writes the run's history, then the snapshot, then output; output between them is covered by the snapshot", () => {
  expect(feed([f(1, "history"), f(0, "history"), f(2), f(3, "snapshot"), f(4)]).written).toEqual(["h0", "h1", "s3", "o4"]);
});
it("a viewer that has the run's history consumes a later burst in order and writes none of it", () => {
  expect(feed([f(0, "history"), f(1, "snapshot"), f(3, "history"), f(2, "history"), f(4, "snapshot"), f(5)]).written).toEqual(["h0", "s1", "s4", "o5"]);
});
it("a viewer that drew the run before any history reached it writes the next burst, once", () => {
  // Prod 2026-10-07: a view that mounted in a hidden tab drew another tab's frames; its own fresh
  // watch, when shown, then brought a burst it consumed and never wrote. In order or not.
  const drawn = feed([f(3, "snapshot"), f(4), f(5, "snapshot")]);
  expect(drawn.written).toEqual(["s3", "o4", "s5"]);
  const burst = feed([f(7, "history"), f(6, "history"), f(8), f(9, "snapshot"), f(10)], drawn.order);
  expect(burst.written).toEqual(["h6", "h7", "s9", "o10"]);
  expect(feed([f(11, "history"), f(12, "snapshot")], burst.order).written).toEqual(["s12"]);
  // The same after frames were lost and it fell out of sync.
  const lost = feed(Array.from({ length: 17 }, (_, i) => f(i + 20)), drawn.order);
  expect(feed([f(40, "history"), f(41, "snapshot")], lost.order).written).toEqual(["h40", "s41"]);
});
it("an empty history frame is all the history there is: later bursts are someone else's", () => {
  const { order, written } = feed([f(0, "history"), f(1, "snapshot")]);
  expect(written).toEqual(["h0", "s1"]);
  expect(order.bare).toBe(false);
  expect(feed([f(0, "snapshot")]).order.bare).toBe(true);
});
it("history that arrives after its snapshot starts the picture over: the history, then all that is written since", () => {
  // Prod 2026-10-08: each frame is its own request from the Mac to the viewer, and the snapshot the
  // Mac sent behind the burst can come first (once by 2.6 s). Such a view dropped the burst and kept
  // only the last screen: about half of all opens.
  expect(feed([f(1, "snapshot"), f(0, "history")]).written).toEqual(["s1", "h0", "s1"]);
  const late = feed([f(2, "snapshot"), f(3), f(1, "history"), f(4), f(0, "history"), f(5)]);
  expect(late.written).toEqual(["s2", "o3", "h1", "s2", "o3", "o4", "h0", "h1", "s2", "o3", "o4", "o5"]);
  expect(late.order.bare).toBe(false);
  // Part of the burst before the snapshot, part after.
  expect(feed([f(0, "history"), f(2, "history"), f(3, "snapshot"), f(4), f(1, "history")]).written)
    .toEqual(["h2", "s3", "o4", "h0", "h1", "h2", "s3", "o4"]);
  // A view that drew the run before its own watch brought the burst, and got that burst's snapshot first.
  expect(feed([f(3, "snapshot"), f(5, "snapshot"), f(4, "history"), f(6)]).written).toEqual(["s3", "s5", "h4", "s5", "o6"]);
});
it("history that arrives late never redraws a viewer whose history is whole, and never twice", () => {
  const whole = feed([f(0, "history"), f(1, "snapshot"), f(2)]);
  // Someone else's burst, its snapshot first: consumed, nothing written again.
  expect(feed([f(4, "snapshot"), f(3, "history"), f(5)], whole.order).written).toEqual(["s4", "o5"]);
  // The same frame again, and one from an older run.
  expect(feed([f(1, "snapshot"), f(0, "history"), f(0, "history"), f(0, "history", 0)]).written).toEqual(["s1", "h0", "s1"]);
  // A chunk nearer the snapshot than the run written replaces it, though no longer: a lost chunk costs only the older part.
  expect(feed([f(0, "history"), f(3, "snapshot"), f(2, "history")]).written).toEqual(["h0", "s3", "h2", "s3"]);
  expect(feed([f(0, "history"), f(1, "history"), f(4, "snapshot"), f(3, "history"), f(3, "history")]).written).toEqual(["h0", "h1", "s4", "h3", "s4"]);
  // An older chunk that does not join the run written makes it no longer; the chunk between them does.
  expect(feed([f(3, "history"), f(4, "snapshot"), f(1, "history")]).written).toEqual(["h3", "s4"]);
  expect(feed([f(3, "history"), f(4, "snapshot"), f(1, "history"), f(2, "history")]).written).toEqual(["h3", "s4", "h1", "h2", "h3", "s4"]);
});
it("history later than a long picture waits for the next snapshot, or is someone else's", () => {
  const long = [f(1, "snapshot"), ...Array.from({ length: 32 }, (_, i) => f(i + 2))];
  const bare = feed(long);
  expect(bare.order.shown).toEqual([]);
  expect(feed([f(0, "history"), f(34, "snapshot")], bare.order).written).toEqual(["h0", "s34"]);
  const partial = feed([f(1, "history"), ...long.map((frame) => ({ ...frame, seq: frame.seq + 1 }))]);
  expect(feed([f(0, "history"), f(35, "snapshot")], partial.order).written).toEqual(["s35"]);
});
it("a lost history chunk costs only the older part", () => {
  expect(feed([f(0, "history"), f(2, "history"), f(3, "snapshot")]).written).toEqual(["h2", "s3"]);
});
it("a new relay run brings its own history; the old run's is ignored", () => {
  const { order } = feed([f(0, "snapshot", 1)]);
  expect(feed([f(5, "history", 1), f(0, "history", 2), f(1, "snapshot", 2)], order).written).toEqual(["h0", "s1"]);
});
it("a ping has no place in the order", () => {
  expect(feed([f(0, "snapshot"), f(1, "ping"), f(1), f(2, "ping")]).written).toEqual(["s0", "o1"]);
});
it("a snapshot that spans frames is written whole, in sync or not", () => {
  expect(feed([f(0, "snapshot"), f(1), f(2, "snapshot"), f(3)]).written).toEqual(["s0", "o1", "s2", "o3"]);
});
