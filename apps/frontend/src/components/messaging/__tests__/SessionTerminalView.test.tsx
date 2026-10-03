import { act, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";
import { SessionTerminalView } from "../SessionTerminalView";

const writes: Uint8Array[] = [];
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80; rows = 24; open() {} reset() {} dispose() {}
  resize(c: number, r: number) { this.cols = c; this.rows = r; }
  write(data: Uint8Array, done?: () => void) { writes.push(data); done?.(); }
} }));
afterEach(() => { writes.length = 0; vi.useRealTimers(); });

const screenEvent = (sessionId: string, kind = "snapshot", seq = 0) => ({ schemaVersion: 1, eventId: `e${seq}`, type: "workspace.session.screen", occurredAt: "now",
  payload: { sessionId, generation: "g1", epoch: 1, seq, cols: 100, rows: 30, kind, data: "G1tI" } }) as LiveEvent;

it("watches while on screen, draws the snapshot read-only, and ignores other sessions", async () => {
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  const watch = vi.fn(async () => undefined);
  const onLive = vi.fn();
  const { unmount } = render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
    onEvent={(h) => { emit = h; return () => {}; }} watch={watch} onLive={onLive} />);
  expect(watch).toHaveBeenCalledWith("s1");
  await act(async () => { await vi.runOnlyPendingTimersAsync(); }); // xterm's dynamic import
  act(() => { emit(screenEvent("other")); emit(screenEvent("s1")); });
  expect(writes.map((w) => Array.from(w))).toEqual([[0x1b, 0x5b, 0x48]]);
  expect(onLive).toHaveBeenCalledWith(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(watch).toHaveBeenCalledTimes(2);
  unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(watch).toHaveBeenCalledTimes(2);
});
