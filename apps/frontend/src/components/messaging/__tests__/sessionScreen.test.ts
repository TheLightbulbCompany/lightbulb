import { expect, it } from "vitest";
import type { SessionScreen } from "@lightbulb/shared";
import { NEW_SCREEN, receiveScreen, type ScreenOrder } from "../sessionScreen";

const f = (seq: number, kind: SessionScreen["kind"] = "output", epoch = 1): SessionScreen =>
  ({ sessionId: "s", generation: "g", epoch, seq, cols: 80, rows: 24, kind, data: "" });
function feed(frames: SessionScreen[], order: ScreenOrder = NEW_SCREEN) {
  const written: number[] = [];
  for (const frame of frames) { const step = receiveScreen(order, frame); order = step.order; written.push(...step.write.map(w => w.seq)); }
  return { order, written };
}

it("writes nothing before a snapshot, then everything in seq order", () => {
  expect(feed([f(5), f(3, "snapshot"), f(4)]).written).toEqual([3, 4, 5]);
});
it("drops duplicates and frames from an older relay run", () => {
  expect(feed([f(0, "snapshot", 2), f(1, "output", 2), f(1, "output", 2), f(7, "output", 1)]).written).toEqual([0, 1]);
});
it("stops writing after a gap it cannot fill, until the next snapshot", () => {
  const gap = [f(0, "snapshot"), ...Array.from({ length: 17 }, (_, i) => f(i + 2))];
  const { order, written } = feed(gap);
  expect(written).toEqual([0]);
  expect(feed([f(30, "snapshot"), f(31)], order).written).toEqual([30, 31]);
});
