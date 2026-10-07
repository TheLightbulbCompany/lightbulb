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
it("stops writing after a gap it cannot fill, until the next snapshot, and takes no history for a run it has drawn", () => {
  const gap = [f(0, "snapshot"), ...Array.from({ length: 17 }, (_, i) => f(i + 2))];
  const { order, written } = feed(gap);
  expect(written).toEqual(["s0"]);
  expect(feed([f(29, "history"), f(30, "snapshot"), f(31)], order).written).toEqual(["s30", "o31"]);
});
it("a new viewer writes the run's history, then the snapshot, then output; output between them is covered by the snapshot", () => {
  expect(feed([f(1, "history"), f(0, "history"), f(2), f(3, "snapshot"), f(4)]).written).toEqual(["h0", "h1", "s3", "o4"]);
});
it("a viewer that has the run's screen consumes history in order and writes none of it", () => {
  expect(feed([f(0, "snapshot"), f(2, "history"), f(1, "history"), f(3, "snapshot"), f(4)]).written).toEqual(["s0", "s3", "o4"]);
});
it("history arriving after its snapshot is never written", () => {
  expect(feed([f(2, "snapshot"), f(0, "history"), f(1, "history"), f(3)]).written).toEqual(["s2", "o3"]);
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
