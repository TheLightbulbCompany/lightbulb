import { readFileSync } from "node:fs";
import path from "node:path";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type LiveEvent } from "@lightbulb/shared";
import { SessionTerminalView } from "../SessionTerminalView";

const writes: Uint8Array[] = [];
const hooks: { data?: (d: string) => void; key?: (e: unknown) => boolean } = {};
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true }; open() {} reset() {} dispose() {}
  onData(cb: (d: string) => void) { hooks.data = cb; }
  attachCustomKeyEventHandler(cb: (e: unknown) => boolean) { hooks.key = cb; }
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
  await act(async () => { await vi.dynamicImportSettled(); }); // xterm's dynamic import
  act(() => { emit(screenEvent("other")); emit(screenEvent("s1")); });
  expect(writes.map((w) => Array.from(w))).toEqual([[0x1b, 0x5b, 0x48]]);
  expect(onLive).toHaveBeenCalledWith(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(watch).toHaveBeenCalledTimes(2);
  unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(watch).toHaveBeenCalledTimes(2);
});

it("goes back to not-live when frames stop, and live again on the next snapshot", async () => {
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  const onLive = vi.fn();
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
    onEvent={(h) => { emit = h; return () => {}; }} watch={async () => undefined} onLive={onLive} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  act(() => { emit(screenEvent("s1")); });
  expect(onLive).toHaveBeenLastCalledWith(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(onLive).toHaveBeenLastCalledWith(false);
  act(() => { emit(screenEvent("s1", "snapshot", 1)); });
  expect(onLive).toHaveBeenLastCalledWith(true);
});

const b64 = (s: string) => btoa(s);
const mount = async (admitted: boolean, input: (d: Uint8Array, e: number) => Promise<unknown>, session: { sessionId: string; generation: string } = { sessionId: "s1", generation: "g1" }) => {
  let emit: (e: LiveEvent) => void = () => {};
  const props = { onEvent: (h: (e: LiveEvent) => void) => { emit = h; return () => {}; }, watch: async () => undefined, onLive: () => {}, input };
  const view = render(<SessionTerminalView session={session} admitted={admitted} {...props} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  act(() => { emit(screenEvent("s1")); });
  return { view, props, session, rerender: (a: boolean, s = session) => view.rerender(<SessionTerminalView session={s} admitted={a} {...props} />) };
};
const type = async (d: string) => { act(() => { hooks.data!(d); }); await act(async () => { await vi.advanceTimersByTimeAsync(50); }); };
const sent = (input: ReturnType<typeof vi.fn>) => input.mock.calls.map(([d, e]) => [btoa(String.fromCharCode(...(d as Uint8Array))), e]);

describe("typing into the live terminal", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal("requestAnimationFrame", (cb: () => void) => setTimeout(cb, 0)); vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id)); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("sends an admitted viewer's keystrokes with the screen epoch", async () => {
    const input = vi.fn(async () => undefined);
    await mount(true, input);
    await type("ls");
    expect(sent(input)).toEqual([[b64("ls"), 1]]);
  });
  it("sends nothing for a viewer who is not admitted", async () => {
    const input = vi.fn(async () => undefined);
    await mount(false, input);
    await type("ls");
    expect(input).not.toHaveBeenCalled();
  });
  it("sends Shift+Enter as ESC CR", async () => {
    const input = vi.fn(async () => undefined);
    await mount(true, input);
    let handled = true;
    act(() => { handled = hooks.key!({ type: "keydown", key: "Enter", shiftKey: true }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(50); });
    expect(handled).toBe(false);
    expect(sent(input)).toEqual([[b64("\x1b\r"), 1]]);
  });
  it("stops at once when admission is taken away", async () => {
    const input = vi.fn(async () => undefined);
    const m = await mount(true, input);
    m.rerender(false);
    await type("ls");
    expect(input).not.toHaveBeenCalled();
  });
  it("keeps typing after an offline Mac (a transient 409)", async () => {
    const input = vi.fn(async () => { throw new ApiError(409, { code: "LOCAL_SESSION_OFFLINE" }); });
    await mount(true, input);
    await type("a"); await type("b");
    expect(input).toHaveBeenCalledTimes(2);
  });
  it("stops typing after a Mac that cannot take it", async () => {
    const input = vi.fn(async () => { throw new ApiError(409, { code: "LOCAL_SESSION_UNSUPPORTED" }); });
    await mount(true, input);
    await type("a"); await type("b");
    expect(input).toHaveBeenCalledTimes(1);
  });
  it("keeps a refusal across a refreshed session object, and clears it for a new generation", async () => {
    const input = vi.fn(async () => { throw new ApiError(409, { code: "LOCAL_SESSION_UNSUPPORTED" }); });
    const { rerender } = await mount(true, input);
    await type("a");
    rerender(true, { sessionId: "s1", generation: "g1" } as never);
    await type("b");
    expect(input).toHaveBeenCalledTimes(1);
    rerender(true, { sessionId: "s1", generation: "g2" } as never);
    await type("c");
    expect(input).toHaveBeenCalledTimes(2);
  });
  it("never sends xterm's own probe replies", async () => {
    const input = vi.fn(async () => undefined);
    await mount(true, input);
    await type("\x1b[?1;2c"); await type("\x1b[12;40R"); await type("\x1b[I");
    expect(input).not.toHaveBeenCalled();
  });
  // WebKit (Safari, the desktop app's web view) crashes its WebContent process when the unseen
  // terminal is taken out of flow: its parent is a column flexbox and its containing block a grid.
  it("keeps the unseen terminal in flow", () => {
    const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
    const rule = /\.session-terminal-view\[data-live="false"\]\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toContain("visibility: hidden");
    expect(rule).not.toMatch(/position\s*:/);
  });
});
