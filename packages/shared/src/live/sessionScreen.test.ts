import { expect, it, test } from "vitest";
import { NEW_SCREEN, receiveScreen, settleScreen, snapshotWaits, type ScreenOrder } from "./sessionScreen";
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
  // (That snapshot waits for the frame below it, so the picture is drawn once.)
  expect(feed([f(3, "snapshot"), f(5, "snapshot"), f(4, "history"), f(6)]).written).toEqual(["s3", "h4", "s5", "o6"]);
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

// Prod 2026-10-08, review of #3894: the Mac sends the output it held and then the snapshot as two
// frames, each its own request, and the snapshot can come first. Written at once it covered the
// screen and the output was dropped as old: an inline Codex session's lines, scrolled off the top by
// that output, never reached the viewer's scrollback.
const names = (write: SessionScreen[]) => write.map((w) => `${w.kind[0]}${w.seq}`);
it("a snapshot ahead of an in-sync viewer's place waits for the output below it, in either arrival order", () => {
  expect(feed([f(0, "snapshot"), f(1), f(2), f(3, "snapshot"), f(4)]).written).toEqual(["s0", "o1", "o2", "s3", "o4"]);
  const early = feed([f(0, "snapshot"), f(1), f(3, "snapshot")]);
  expect(early.written).toEqual(["s0", "o1"]);
  expect(snapshotWaits(early.order)).toBe(true);
  const filled = feed([f(4), f(2)], early.order);
  expect(filled.written).toEqual(["o2", "s3", "o4"]);
  expect(snapshotWaits(filled.order)).toBe(false);
  // Two frames below it, the nearer one first; and two snapshots that wait, each in its place.
  expect(feed([f(0, "snapshot"), f(3, "snapshot"), f(2), f(1)]).written).toEqual(["s0", "o1", "o2", "s3"]);
  expect(feed([f(0, "snapshot"), f(2, "snapshot"), f(4, "snapshot"), f(1), f(3)]).written).toEqual(["s0", "o1", "s2", "o3", "s4"]);
  // A new viewer's burst, its snapshot ahead of its history: someone already watching writes that snapshot once the burst is here.
  expect(feed([f(0, "history"), f(1, "snapshot"), f(4, "snapshot"), f(2, "history"), f(3, "history"), f(5)]).written).toEqual(["h0", "s1", "s4", "o5"]);
});
it("a waiting snapshot is written as it is when the frames below it never come: a repair, as before", () => {
  const early = feed([f(0, "snapshot"), f(2, "snapshot"), f(3)]);
  expect(early.written).toEqual(["s0"]);
  const settled = settleScreen(early.order);
  expect(names(settled.write)).toEqual(["s2", "o3"]);
  expect(snapshotWaits(settled.order)).toBe(false);
  // The frame it waited for is old now, and there is nothing left to settle.
  expect(feed([f(1), f(4)], settled.order).written).toEqual(["o4"]);
  expect(settleScreen(settled.order)).toEqual({ order: settled.order, write: [] });
});
it("no snapshot waits for a viewer with no picture, a new run, or a viewer out of sync", () => {
  expect(snapshotWaits(feed([f(7, "snapshot")]).order)).toBe(false);
  expect(feed([f(7, "snapshot")]).written).toEqual(["s7"]);
  const run = feed([f(0, "snapshot"), f(1)]);
  expect(feed([f(5, "snapshot", 2)], run.order).written).toEqual(["s5"]);
  const lost = feed(Array.from({ length: 17 }, (_, i) => f(i + 3)), run.order);
  expect(lost.order.synced).toBe(false);
  expect(feed([f(30, "snapshot")], lost.order).written).toEqual(["s30"]);
  // More frames above a waiting snapshot than a view holds: it is written then, with them. And it does not outlive its run.
  const waiting = feed([f(3, "snapshot")], run.order);
  expect(feed(Array.from({ length: 17 }, (_, i) => f(i + 4)), waiting.order).written).toEqual(["s3", ...Array.from({ length: 17 }, (_, i) => `o${i + 4}`)]);
  const next = feed([f(0, "snapshot", 2)], waiting.order);
  expect(next.written).toEqual(["s0"]);
  expect(snapshotWaits(next.order)).toBe(false);
});

it("a viewer repaired by a snapshot over frames that never came asks for its lines and starts over from the burst", () => {
  // Prod 2026-10-08: the Mac's connection loop stalled 6.1 s; the frames of 4 Codex blocks never
  // came, two snapshots repaired the screen, and those 82 rows were in no scrollback of the viewer.
  const lines = (seq: number): SessionScreen => ({ ...f(seq, "history"), data: "bGluZXM=" });
  const whole = feed([lines(0), f(1, "snapshot"), f(2)]);
  expect(whole.written).toEqual(["h0", "s1", "o2"]);
  // Seq 3 is lost. Output ahead of it waits; the beat's snapshot waits its turn too, then is the repair.
  const waiting = feed([f(4), f(5, "snapshot")], whole.order);
  expect(waiting.written).toEqual([]);
  const repair = settleScreen(waiting.order);
  expect(repair.write.map((w) => `${w.kind[0]}${w.seq}`)).toEqual(["s5"]);
  expect(repair.lost).toBe(true);
  // What it was first drawn from is not written again: the next snapshot alone changes nothing.
  expect(feed([f(6), f(7, "snapshot")], repair.order).written).toEqual(["o6", "s7"]);
  // A burst with nothing in it ("nothing above the screen", or a budget with no room) costs it no line it has.
  const empty = feed([f(6, "history"), f(7, "snapshot")], repair.order);
  expect(empty.written).toEqual(["s7"]);
  // The burst its fresh watch brings: history first, the picture over it. Once.
  const burst = feed([lines(8), lines(9), f(10, "snapshot"), f(11)], empty.order);
  expect(burst.written).toEqual(["h8", "h9", "s10", "o11"]);
  expect(feed([lines(12), f(13, "snapshot")], burst.order).written).toEqual(["s13"]);
  // A viewer that missed nothing says nothing and writes none of that burst.
  const other = feed([f(3), f(4), f(5, "snapshot")], whole.order);
  expect(other.written).toEqual(["o3", "o4", "s5"]);
  expect(receiveScreen(whole.order, f(3)).lost).toBeUndefined();
  expect(feed([lines(6), lines(7), f(8, "snapshot")], other.order).written).toEqual(["s8"]);
  // Out of sync (more lost than is held): the snapshot that brings it back is a repair too.
  let order = whole.order;
  for (let seq = 4; seq <= 20; seq += 1) order = receiveScreen(order, f(seq)).order;
  expect(order.synced).toBe(false);
  expect(receiveScreen(order, f(21, "snapshot")).lost).toBe(true);
  // A new run is no repair: it draws everything.
  expect(receiveScreen(whole.order, f(0, "snapshot", 2)).lost).toBeUndefined();
});
