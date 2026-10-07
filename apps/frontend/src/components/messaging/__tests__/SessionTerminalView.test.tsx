import { readFileSync } from "node:fs";
import path from "node:path";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type LiveEvent } from "@lightbulb/shared";
import { SessionTerminalView, terminalScale } from "../SessionTerminalView";

const writes: Uint8Array[] = [];
const hooks: { data?: (d: string) => void; key?: (e: unknown) => boolean; term?: { cols: number; rows: number };
  options?: { scrollback?: number }; resets: number; wheel?: (e: { deltaY: number }) => boolean; screen?: { w: number; h: number } } = { resets: 0 };
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true };
  constructor(options: { scrollback?: number }) { hooks.term = this; hooks.options = options; }
  // The drawn grid: xterm sizes `.xterm-screen` to cols x rows, whatever the box around it is.
  open(el: HTMLElement) {
    const shown = el.appendChild(document.createElement("div")); shown.className = "xterm-screen";
    Object.defineProperty(shown, "offsetWidth", { get: () => hooks.screen?.w ?? 0 });
    Object.defineProperty(shown, "offsetHeight", { get: () => hooks.screen?.h ?? 0 });
  }
  reset() { hooks.resets += 1; } dispose() {}
  attachCustomWheelEventHandler(cb: (e: { deltaY: number }) => boolean) { hooks.wheel = cb; }
  onData(cb: (d: string) => void) { hooks.data = cb; }
  attachCustomKeyEventHandler(cb: (e: unknown) => boolean) { hooks.key = cb; }
  resize(c: number, r: number) { this.cols = c; this.rows = r; }
  write(data: Uint8Array, done?: () => void) { writes.push(data); done?.(); }
} }));
afterEach(() => { writes.length = 0; hooks.resets = 0; hooks.screen = undefined; window.localStorage.clear(); vi.useRealTimers(); });
const CLEAR = Array.from(new TextEncoder().encode("\x1b[?1049l\x1b[r\x1b[2J\x1b[H"));

const screenEvent = (sessionId: string, kind = "snapshot", seq = 0, epoch = 1, data = "G1tI") => ({ schemaVersion: 1, eventId: `e${seq}`, type: "workspace.session.screen", occurredAt: "now",
  payload: { sessionId, generation: "g1", epoch, seq, cols: 100, rows: 30, kind, data } }) as LiveEvent;

it("watches while on screen, draws the snapshot read-only, and ignores other sessions", async () => {
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  const watch = vi.fn(async () => undefined);
  const onLive = vi.fn();
  const { unmount } = render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
    onEvent={(h) => { emit = h; return () => {}; }} watch={watch} onLive={onLive} />);
  expect(watch).toHaveBeenCalledWith("s1", true); // a mount has no screen: it asks for one
  await act(async () => { await vi.dynamicImportSettled(); }); // xterm's dynamic import
  act(() => { emit(screenEvent("other")); emit(screenEvent("s1")); });
  expect(writes.map((w) => Array.from(w))).toEqual([[...CLEAR, 0x1b, 0x5b, 0x48]]);
  expect(onLive).toHaveBeenCalledWith(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(watch).toHaveBeenCalledTimes(2);
  expect(watch).toHaveBeenLastCalledWith("s1", false); // a renewal asks for nothing
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

it("says Connecting until the first snapshot, Reconnecting when frames stop, and nothing once live", async () => {
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
    onEvent={(h) => { emit = h; return () => {}; }} watch={async () => undefined} onLive={() => {}} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  expect(screen.getByRole("status").textContent).toBe("Connecting…");
  act(() => { emit(screenEvent("s1")); });
  expect(screen.queryByRole("status")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(screen.getByRole("status").textContent).toBe("Reconnecting…");
  act(() => { emit(screenEvent("s1", "snapshot", 1)); });
  expect(screen.queryByRole("status")).toBeNull();
});

it("tells the viewer when no frame arrives in 30 s, and clears it on the first snapshot", async () => {
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
    onEvent={(h) => { emit = h; return () => {}; }} watch={async () => undefined} onLive={() => {}} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  expect(screen.getByRole("status").textContent).toBe("Connecting…");
  await act(async () => { await vi.advanceTimersByTimeAsync(30_001); });
  expect(screen.getByRole("status").textContent).toMatch(/Nothing has arrived from the Mac in 30 seconds/);
  act(() => { emit(screenEvent("s1")); });
  expect(screen.queryByRole("status")).toBeNull();
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

const mountView = async (props: Partial<Parameters<typeof SessionTerminalView>[0]> = {}) => {
  let emit: (e: LiveEvent) => void = () => {};
  const onLive = vi.fn();
  const watch = vi.fn(async () => undefined);
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={(h) => { emit = h; return () => {}; }}
    watch={watch} onLive={onLive} {...props} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  return { emit: (e: LiveEvent) => act(() => { emit(e); }), onLive, watch };
};

it("writes history under the first snapshot, keeps it when later snapshots redraw, and starts over on a new run", async () => {
  const { emit } = await mountView();
  expect(hooks.options?.scrollback).toBe(5000);
  emit(screenEvent("s1", "history", 0, 1, btoa("old\r\n")));
  emit(screenEvent("s1", "snapshot", 1));
  expect(writes.map((w) => Array.from(w))).toEqual([Array.from(new TextEncoder().encode("old\r\n")), [...CLEAR, 0x1b, 0x5b, 0x48]]);
  emit(screenEvent("s1", "snapshot", 2));
  expect(hooks.resets).toBe(1);
  emit(screenEvent("s1", "snapshot", 0, 2));
  expect(hooks.resets).toBe(2);
});

it("a ping keeps a quiet terminal live and draws nothing", async () => {
  vi.useFakeTimers();
  const { emit, onLive } = await mountView();
  emit(screenEvent("s1"));
  for (let i = 0; i < 3; i += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    emit(screenEvent("s1", "ping", 1, 1, ""));
  }
  expect(onLive).toHaveBeenLastCalledWith(true);
  expect(writes).toHaveLength(1);
});

it("a ping that matches the screen brings a stale terminal back live", async () => {
  vi.useFakeTimers();
  const { emit, onLive } = await mountView();
  emit(screenEvent("s1"));
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(onLive).toHaveBeenLastCalledWith(false);
  expect(screen.getByRole("status").textContent).toBe("Reconnecting…");
  emit(screenEvent("s1", "ping", 1, 1, ""));
  expect(onLive).toHaveBeenLastCalledWith(true);
  expect(screen.queryByRole("status")).toBeNull();
});

it("a ping ahead of the screen (a frame was lost) does not keep it live", async () => {
  vi.useFakeTimers();
  const { emit, onLive } = await mountView();
  emit(screenEvent("s1"));
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
  emit(screenEvent("s1", "ping", 2, 1, "")); // seq 1 never arrived
  await act(async () => { await vi.advanceTimersByTimeAsync(11_000); });
  expect(onLive).toHaveBeenLastCalledWith(false);
  emit(screenEvent("s1", "ping", 2, 1, ""));
  expect(onLive).toHaveBeenLastCalledWith(false); // still waiting for a snapshot
});

it("the viewer's own size never reaches the Mac or changes the terminal's size", async () => {
  let resized: () => void = () => {};
  vi.stubGlobal("ResizeObserver", class { constructor(cb: () => void) { resized = cb; } observe() {} disconnect() {} });
  const input = vi.fn(async () => undefined);
  const { emit, watch } = await mountView({ input, admitted: true });
  emit(screenEvent("s1"));
  act(() => { resized(); resized(); });
  expect(input).not.toHaveBeenCalled();
  expect(watch).toHaveBeenCalledTimes(1);
  expect([hooks.term?.cols, hooks.term?.rows]).toEqual([100, 30]);
  vi.unstubAllGlobals();
});

describe("a readable terminal in a narrow pane", () => {
  // The acceptance finding: a 124x69 session in a 984 px window drew at scale 0.38, about 5 px text.
  const GRID = { w: 968, h: 1173 }; // 124 x 69 at 13 px Menlo
  const PANE = { w: 660, h: 390 };
  /** The scroll box, given a size (jsdom lays nothing out). */
  const box = (size = PANE, scrollHeight = 880) => {
    const el = document.querySelector<HTMLElement>(".session-terminal-scroll")!;
    Object.defineProperty(el, "clientWidth", { configurable: true, get: () => size.w });
    Object.defineProperty(el, "clientHeight", { configurable: true, get: () => size.h });
    Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => scrollHeight });
    return el;
  };
  const drawn = () => document.querySelector<HTMLElement>(".session-terminal-scroll > div > div")!.style.transform;
  const open = async (props: Partial<Parameters<typeof SessionTerminalView>[0]> = {}) => {
    hooks.screen = GRID;
    let emit: (e: LiveEvent) => void = () => {};
    const view = render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={(h) => { emit = h; return () => {}; }}
      watch={async () => undefined} onLive={() => {}} {...props} />);
    await act(async () => { await vi.dynamicImportSettled(); });
    const el = box();
    return { el, view, emit: (e: LiveEvent) => act(() => { emit(e); }) };
  };

  it("fits the width, never below the readable floor, and never above full size", () => {
    expect(terminalScale("readable", GRID, PANE)).toBe(0.75); // 660 / 968 = 0.68: the floor holds
    expect(terminalScale("readable", GRID, { w: 850, h: 390 })).toBeCloseTo(850 / 968);
    expect(terminalScale("readable", GRID, { w: 2000, h: 390 })).toBe(1);
    expect(terminalScale("fit", GRID, PANE)).toBeCloseTo(390 / 1173); // the old whole-screen fit
    expect(terminalScale("fit", GRID, { w: 2000, h: 2000 })).toBe(1);
  });

  it("draws at the readable scale inside a box that scrolls both ways", async () => {
    const { emit } = await open();
    emit(screenEvent("s1"));
    expect(drawn()).toBe("scale(0.75)");
    // The wrapper takes the scaled size, so the box scrolls exactly as far as the picture reaches.
    const sized = document.querySelector<HTMLElement>(".session-terminal-scroll > div")!;
    expect([sized.style.width, sized.style.height]).toEqual(["726px", "879px"]);
    const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
    expect(/\.session-terminal-scroll\s*\{([^}]*)\}/.exec(css)?.[1]).toContain("overflow: auto");
  });

  it("keeps the bottom of the screen in view as output arrives, until the viewer scrolls away", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    expect(el.scrollTop).toBe(880);
    el.scrollTop = 100; fireEvent.scroll(el); // the viewer reads further up
    emit(screenEvent("s1", "output", 1));
    expect(el.scrollTop).toBe(100);
    el.scrollTop = 490; fireEvent.scroll(el); // back at the bottom: 490 + 390 = 880
    el.scrollTop = 0;
    emit(screenEvent("s1", "output", 2));
    expect(el.scrollTop).toBe(880);
  });

  it("the wheel moves the box while it can, then the terminal's own scrollback", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    // false = xterm leaves the event alone and the browser scrolls the box; true = xterm scrolls its history.
    el.scrollTop = 490;
    expect(hooks.wheel?.({ deltaY: -30 })).toBe(false); // up: the rest of this screen first
    expect(hooks.wheel?.({ deltaY: 30 })).toBe(true); // down at the bottom: back toward the tail
    el.scrollTop = 0;
    expect(hooks.wheel?.({ deltaY: -30 })).toBe(true); // up at the top: into history
    expect(hooks.wheel?.({ deltaY: 30 })).toBe(false);
  });

  it("has a Fit / Readable switch that is remembered for this viewer", async () => {
    const { emit, view } = await open();
    emit(screenEvent("s1"));
    const group = screen.getByRole("group", { name: "Terminal size" });
    expect(group.closest(".session-terminal-scroll")).toBeNull(); // its own strip, never over the text
    expect(screen.getByRole("button", { name: "Readable" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(screen.getByRole("button", { name: "Fit" }).getAttribute("aria-pressed")).toBe("true");
    expect(drawn()).toBe(`scale(${390 / 1173})`);
    view.unmount();
    await open();
    expect(screen.getByRole("button", { name: "Fit" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("works when this browser's storage is blocked", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    const { emit } = await open();
    emit(screenEvent("s1"));
    fireEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(screen.getByRole("button", { name: "Fit" }).getAttribute("aria-pressed")).toBe("true");
    get.mockRestore(); set.mockRestore();
  });
});
