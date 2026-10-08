import { readFileSync } from "node:fs";
import path from "node:path";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type LiveEvent } from "@lightbulb/shared";
import { SessionTerminalView, terminalScale } from "../SessionTerminalView";

const writes: Uint8Array[] = [];
const hooks: { data?: (d: string) => void; key?: (e: unknown) => boolean; term?: { cols: number; rows: number };
  options?: { scrollback?: number }; resets: number; screen?: { w: number; h: number }; scrolled: number[]; binary?: boolean;
  /** Set: writes wait, as in xterm, until `drain` parses them in order; `did` is what the terminal then did. */
  later?: (() => void)[]; did: string[] } = { resets: 0, scrolled: [], did: [] };
const drain = () => { for (const step of hooks.later?.splice(0) ?? []) step(); };
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true };
  // 100 rows of history above the screen. A test that sets `baseY` gets xterm's own moves: a reset empties
  // the buffer, each line feed adds a row (the view follows at the tail), and a scroll stops at the tail.
  buffer: { active: { viewportY: number; baseY?: number } } = { active: { viewportY: 100 } };
  scrollToLine(n: number) { hooks.did.push(`line ${n}`); this.buffer.active.viewportY = Math.min(n, this.buffer.active.baseY ?? n); }
  scrollLines(n: number) { hooks.scrolled.push(n); this.buffer.active.viewportY = Math.max(0, Math.min(100, this.buffer.active.viewportY + n)); }
  onBinary() { hooks.binary = true; }
  constructor(options: { scrollback?: number }) { hooks.term = this; hooks.options = options; }
  // The drawn grid: xterm sizes `.xterm-screen` to cols x rows, whatever the box around it is.
  open(el: HTMLElement) {
    const shown = el.appendChild(document.createElement("div")); shown.className = "xterm-screen";
    Object.defineProperty(shown, "offsetWidth", { get: () => hooks.screen?.w ?? 0 });
    Object.defineProperty(shown, "offsetHeight", { get: () => hooks.screen?.h ?? 0 });
  }
  reset() { hooks.resets += 1; hooks.did.push("reset"); if (this.buffer.active.baseY !== undefined) this.buffer.active.baseY = this.buffer.active.viewportY = 0; }
  dispose() {}
  onData(cb: (d: string) => void) { hooks.data = cb; }
  attachCustomKeyEventHandler(cb: (e: unknown) => boolean) { hooks.key = cb; }
  resize(c: number, r: number) { this.cols = c; this.rows = r; }
  // An empty write only keeps a reset or a resize in its place in the order.
  write(data: Uint8Array | string, done?: () => void) {
    const parse = () => {
      if (typeof data !== "string") {
        const text = new TextDecoder().decode(data).replace(/\x1b[^a-zA-Z]*[a-zA-Z]/g, "");
        writes.push(data); hooks.did.push(text);
        const at = this.buffer.active;
        if (at.baseY !== undefined) { const tail = at.viewportY >= at.baseY; at.baseY += text.split("\n").length - 1; if (tail) at.viewportY = at.baseY; }
      }
      done?.();
    };
    if (hooks.later) hooks.later.push(parse); else parse();
  }
} }));
afterEach(() => { writes.length = 0; hooks.resets = 0; hooks.later = undefined; hooks.did = []; hooks.scrolled = []; hooks.binary = undefined; hooks.screen = undefined; window.localStorage.clear(); vi.useRealTimers(); });
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

it("a hidden tab stops watching, and asks for one fresh screen when it is shown again", async () => {
  vi.useFakeTimers();
  let state: DocumentVisibilityState = "visible";
  const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state);
  const show = (next: DocumentVisibilityState) => act(() => { state = next; document.dispatchEvent(new Event("visibilitychange")); });
  const watch = vi.fn(async () => undefined);
  const { unmount } = render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
    onEvent={() => () => {}} watch={watch} onLive={vi.fn()} />);
  expect(watch.mock.calls).toEqual([["s1", true]]);
  show("hidden");
  // A hidden tab's timers run about once a minute: each late renewal found the 45 s lease lapsed
  // and counted as a new viewer, a history burst a minute. Hidden, it asks for nothing at all.
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000); });
  expect(watch).toHaveBeenCalledTimes(1);
  show("visible");
  expect(watch.mock.calls).toEqual([["s1", true], ["s1", true]]); // the lease lapsed: one fresh screen
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(watch.mock.calls.slice(2)).toEqual([["s1", false]]); // and renewals again, one timer
  unmount();
  show("hidden"); show("visible");
  expect(watch).toHaveBeenCalledTimes(3);
  spy.mockRestore();
});

it("a view mounted in a hidden tab asks for nothing until the tab is shown", async () => {
  vi.useFakeTimers();
  let state: DocumentVisibilityState = "hidden";
  const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state);
  const watch = vi.fn(async () => undefined);
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={() => () => {}} watch={watch} onLive={vi.fn()} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(watch).not.toHaveBeenCalled();
  act(() => { state = "visible"; document.dispatchEvent(new Event("visibilitychange")); });
  expect(watch.mock.calls).toEqual([["s1", true]]);
  spy.mockRestore();
});

it("a view that drew frames before it asked starts over with the history its own ask brings, and only then", async () => {
  // Prod 2026-10-07: a view mounted while its tab was hidden drew the frames another tab's watch brought
  // (26 lines, no scrollback). Shown, it asked for a fresh screen, and the history that came was never written.
  vi.useFakeTimers();
  let state: DocumentVisibilityState = "hidden";
  const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state);
  let emit: (e: LiveEvent) => void = () => {};
  const watch = vi.fn(async () => undefined);
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={(h) => { emit = h; return () => {}; }} watch={watch} onLive={vi.fn()} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  act(() => { emit(screenEvent("s1", "snapshot", 3)); emit(screenEvent("s1", "output", 4, 1, btoa("live"))); });
  expect(watch).not.toHaveBeenCalled();
  expect([writes.length, hooks.resets]).toEqual([2, 1]);
  act(() => { state = "visible"; document.dispatchEvent(new Event("visibilitychange")); });
  expect(watch.mock.calls).toEqual([["s1", true]]);
  act(() => { emit(screenEvent("s1", "history", 5, 1, btoa("one\r\n"))); emit(screenEvent("s1", "history", 6, 1, btoa("two\r\n"))); emit(screenEvent("s1", "snapshot", 7)); });
  const text = (s: string) => Array.from(new TextEncoder().encode(s));
  expect(hooks.resets).toBe(2); // once more, for the burst: its history is the scrollback now
  expect(writes.slice(2).map((w) => Array.from(w))).toEqual([text("one\r\n"), text("two\r\n"), [...CLEAR, 0x1b, 0x5b, 0x48]]);
  // The next burst is for someone else who started watching: this view's scrollback stays.
  act(() => { emit(screenEvent("s1", "history", 8, 1, btoa("one\r\n"))); emit(screenEvent("s1", "snapshot", 9)); });
  expect([writes.length, hooks.resets]).toEqual([6, 2]);
  spy.mockRestore();
});

it("asks for the kept picture once when the session it was watching ends, and draws it in place", async () => {
  // The backend refuses the watch (409) between the end and the Mac's report of the kept screen, and drops
  // the Mac's frames; a renewal after that is no new viewer, so nothing sent the kept picture (prod 2026-10-07).
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  const watch = vi.fn(async () => undefined);
  const props = { session: { sessionId: "s1", generation: "g1" }, onEvent: (h: (e: LiveEvent) => void) => { emit = h; return () => {}; }, watch, onLive: vi.fn() };
  const { rerender } = render(<SessionTerminalView {...props} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  act(() => { emit(screenEvent("s1")); });
  const term = hooks.term;
  watch.mockImplementation(async () => { throw new ApiError(409, { code: "LOCAL_SESSION_OFFLINE" }); });
  rerender(<SessionTerminalView {...props} kept={false} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(watch.mock.calls).toEqual([["s1", true], ["s1", false]]); // refused, and it goes on renewing
  expect(screen.queryByRole("status")).toBeNull(); // the last live picture stays, with no Connecting
  watch.mockImplementation(async () => undefined);
  rerender(<SessionTerminalView {...props} kept />);
  expect(watch.mock.calls.slice(2)).toEqual([["s1", true]]); // the kept screen is there: one fresh ask
  rerender(<SessionTerminalView {...props} kept />);
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
  expect(watch.mock.calls.slice(3)).toEqual([["s1", false]]);
  act(() => { emit(screenEvent("s1", "snapshot", 5, 1, "a2VwdA==")); });
  // The frames between were dropped while the watch was refused: the picture waits for them, briefly.
  await act(async () => { await vi.advanceTimersByTimeAsync(300); });
  expect(hooks.term).toBe(term); // the same terminal,
  expect(hooks.resets).toBe(1); // started over only for its first frame
  expect(Array.from(writes[1])).toEqual([...CLEAR, ...new TextEncoder().encode("kept")]);
  expect(screen.queryByRole("status")).toBeNull();
});

it("a hidden tab asks for the kept picture when it is shown, not before", async () => {
  vi.useFakeTimers();
  let state: DocumentVisibilityState = "visible";
  const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state);
  const show = (next: DocumentVisibilityState) => act(() => { state = next; document.dispatchEvent(new Event("visibilitychange")); });
  const watch = vi.fn(async () => undefined);
  const props = { session: { sessionId: "s1", generation: "g1" }, onEvent: () => () => {}, watch, onLive: vi.fn() };
  const { rerender } = render(<SessionTerminalView {...props} />);
  show("hidden");
  rerender(<SessionTerminalView {...props} kept />);
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(watch).toHaveBeenCalledTimes(1);
  show("visible");
  expect(watch.mock.calls).toEqual([["s1", true], ["s1", true]]);
  spy.mockRestore();
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

it("a hidden tab only says Connecting: the 30 s clock runs while the tab is shown and watching", async () => {
  vi.useFakeTimers();
  let state: DocumentVisibilityState = "hidden";
  const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state);
  const show = (next: DocumentVisibilityState) => act(() => { state = next; document.dispatchEvent(new Event("visibilitychange")); });
  const status = () => screen.getByRole("status").textContent;
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={() => () => {}} watch={async () => undefined} onLive={() => {}} />);
  // Hidden, it sends no watch, so nothing can arrive: that is not the Mac's doing.
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000); });
  expect(status()).toBe("Connecting…");
  show("visible");
  await act(async () => { await vi.advanceTimersByTimeAsync(29_000); });
  expect(status()).toBe("Connecting…"); // the clock started when the watch was sent, not at mount
  await act(async () => { await vi.advanceTimersByTimeAsync(1_001); });
  expect(status()).toMatch(/Nothing has arrived from the Mac in 30 seconds/);
  show("hidden");
  expect(status()).toBe("Connecting…");
  spy.mockRestore();
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
  // A click, drag or wheel in a Lightbulb view is not typing: with mouse tracking on (Claude Code
  // turns it on) xterm writes each as a report through onData, which an admitted typist would send.
  it("never sends a mouse report, in any encoding, and keeps the typing around it", async () => {
    const input = vi.fn(async () => undefined);
    await mount(true, input);
    await type("\x1b[<0;10;5M"); await type("\x1b[<0;10;5m"); await type("\x1b[<64;10;5M"); // SGR: press, release, wheel
    await type("\x1b[M !!"); await type("\x1b[32;10;5M"); // X10 and urxvt
    expect(input).not.toHaveBeenCalled();
    await type("a\x1b[<35;120;40Mb");
    expect(sent(input)).toEqual([[b64("ab"), 1]]);
    expect(hooks.binary).toBeUndefined(); // X10's binary reports have no listener at all
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

it("history that arrives after its snapshot is written: the terminal starts over with it, then the screen again", async () => {
  // Prod 2026-10-08: the Mac sends the burst, then the snapshot, each as its own request; when the
  // snapshot came first the view showed 30 rows with no scrollback (about half of all opens).
  const { emit } = await mountView();
  emit(screenEvent("s1", "snapshot", 1));
  expect([writes.length, hooks.resets]).toEqual([1, 1]);
  emit(screenEvent("s1", "history", 0, 1, btoa("old\r\n")));
  expect(hooks.resets).toBe(2);
  expect(writes.slice(1).map((w) => new TextDecoder().decode(w).includes("old"))).toEqual([true, false]);
  emit(screenEvent("s1", "history", 0, 1, btoa("old\r\n")));
  expect([writes.length, hooks.resets]).toEqual([3, 2]);
});

it("a picture that starts over leaves a reader who scrolled up on their line", async () => {
  // A session that ends on a picture of lines its viewers have (desktop: `startOver`) comes as a new run:
  // history, then the kept picture. The reset behind it goes to the tail, which pulled a reader down.
  const { emit } = await mountView();
  emit(screenEvent("s1", "history", 0, 1, btoa("old,")));
  emit(screenEvent("s1", "snapshot", 1, 1, btoa("live")));
  const buffer = (hooks.term as unknown as { buffer: { active: { viewportY: number; baseY?: number } } }).buffer.active;
  buffer.baseY = 100; buffer.viewportY = 40; // reading 60 rows above the tail
  hooks.did = [];
  emit(screenEvent("s1", "history", 0, 2, btoa("old,")));
  emit(screenEvent("s1", "snapshot", 1, 2, btoa("kept")));
  expect(hooks.did).toEqual(["reset", "old,", "kept", "line 40"]);
  // Once: the 30 s resync redraws the screen and moves nobody.
  emit(screenEvent("s1", "snapshot", 2, 2, btoa("kept")));
  expect(hooks.did).toEqual(["reset", "old,", "kept", "line 40", "kept"]);
  // A reader at the tail stays at the tail.
  buffer.viewportY = 100; hooks.did = [];
  emit(screenEvent("s1", "history", 0, 3, btoa("old,")));
  emit(screenEvent("s1", "snapshot", 1, 3, btoa("kept")));
  expect(hooks.did).toEqual(["reset", "old,", "kept"]);
});

/** A view that drew run 1 (100 rows of history), with its reader `at` rows from the top. `did` names each history write "history". */
const reading = async (at: number) => {
  const view = await mountView();
  view.emit(screenEvent("s1", "history", 0, 1, btoa("\n".repeat(100))));
  view.emit(screenEvent("s1", "snapshot", 1, 1, btoa("live")));
  const buffer = (hooks.term as unknown as { buffer: { active: { viewportY: number; baseY: number } } }).buffer.active;
  buffer.baseY = 100; buffer.viewportY = at;
  hooks.did = [];
  return { ...view, buffer, did: () => hooks.did.map((d) => (d.trim() ? d : "history")) };
};

it("a reader who scrolled up stays on their line when the new run's snapshot comes before its history", async () => {
  // Prod 2026-10-08 (web b786ed9): the session ended, the kept picture's snapshot reached the page before its
  // history, and a reader at row 30 of 80 ended at the tail. The snapshot alone is one screen: the line was
  // spent there, and the history that then started the picture over found the reader at the tail.
  const { emit, buffer, did } = await reading(30);
  emit(screenEvent("s1", "snapshot", 1, 2, btoa("kept")));
  expect([did(), buffer.viewportY]).toEqual([["reset", "kept", "line 30"], 0]);
  emit(screenEvent("s1", "history", 0, 2, btoa("\n".repeat(100))));
  expect([did().slice(3), buffer.viewportY]).toEqual([["reset", "history", "kept", "line 30"], 30]);
  // Once: the 30 s resync redraws the screen and moves nobody, wherever they went since.
  buffer.viewportY = 50; hooks.did = [];
  emit(screenEvent("s1", "snapshot", 2, 2, btoa("kept")));
  expect([did(), buffer.viewportY]).toEqual([["kept"], 50]);
});

it("a reader stays on their line through history that comes late in two parts", async () => {
  // The newer part first: the picture starts over twice, and the first time it is shorter than the reader's line.
  const { emit, buffer, did } = await reading(70);
  emit(screenEvent("s1", "snapshot", 2, 2, btoa("kept")));
  emit(screenEvent("s1", "history", 1, 2, btoa("\n".repeat(50))));
  expect([did().slice(3), buffer.viewportY]).toEqual([["reset", "history", "kept", "line 70"], 50]);
  emit(screenEvent("s1", "history", 0, 2, btoa("\n".repeat(50))));
  expect([did().slice(7), buffer.viewportY]).toEqual([["reset", "history", "history", "kept", "line 70"], 70]);
});

it("a reader who moved between two late parts of history stays where they went; one at the tail stays at the tail", async () => {
  const { emit, buffer, did } = await reading(70);
  emit(screenEvent("s1", "snapshot", 2, 2, btoa("kept")));
  emit(screenEvent("s1", "history", 1, 2, btoa("\n".repeat(50))));
  buffer.viewportY = 20; hooks.did = []; // scrolled up in the shorter picture
  emit(screenEvent("s1", "history", 0, 2, btoa("\n".repeat(50))));
  expect([did(), buffer.viewportY]).toEqual([["reset", "history", "history", "kept", "line 20"], 20]);
  // At the tail when the next run starts: nothing puts them back on an old line.
  buffer.viewportY = buffer.baseY; hooks.did = [];
  emit(screenEvent("s1", "snapshot", 1, 3, btoa("kept")));
  emit(screenEvent("s1", "history", 0, 3, btoa("\n".repeat(100))));
  expect([did(), buffer.viewportY]).toEqual([["reset", "kept", "reset", "history", "kept"], 100]);
});

it("a redraw starts after the bytes written before it: xterm resets at once but parses writes later", async () => {
  // xterm 5.5.0 keeps queued writes through reset(). Two late history chunks in one tick each start the
  // picture over; reset at once, both resets ran first and the two redraws then drew one after the other.
  const { emit } = await mountView();
  hooks.later = [];
  emit(screenEvent("s1", "snapshot", 2, 1, btoa("screen")));
  emit(screenEvent("s1", "history", 1, 1, btoa("new,")));
  emit(screenEvent("s1", "history", 0, 1, btoa("old,")));
  expect(hooks.did).toEqual([]);
  act(drain);
  expect(hooks.did).toEqual(["reset", "screen", "reset", "new,", "screen", "reset", "old,", "new,", "screen"]);
  expect([hooks.term!.cols, hooks.term!.rows]).toEqual([100, 30]);
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

  /** A wheel over the terminal, as the browser delivers it: at xterm's own element, inside the box. */
  const wheel = (init: WheelEventInit) => {
    const target = document.querySelector<HTMLElement>(".xterm-screen")!;
    const reached = vi.fn(); // xterm's listener: with mouse tracking on it reports the wheel and cancels it
    target.addEventListener("wheel", reached);
    const event = new WheelEvent("wheel", { bubbles: true, cancelable: true, ...init });
    act(() => { target.dispatchEvent(event); });
    target.removeEventListener("wheel", reached);
    return { reached: reached.mock.calls.length > 0, prevented: event.defaultPrevented };
  };

  // The prod finding: Claude Code turns mouse tracking on, xterm then takes every wheel as a mouse
  // report and cancels it, so neither the box nor the scrollback ever moved.
  it("the wheel never reaches xterm: the box moves while it can, then the terminal's own scrollback", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    el.scrollTop = 490; // the bottom of the box
    expect(wheel({ deltaY: -90 })).toEqual({ reached: false, prevented: false }); // up: the browser scrolls the box
    expect(hooks.scrolled).toEqual([]);
    el.scrollTop = 0;
    expect(wheel({ deltaY: 90 })).toEqual({ reached: false, prevented: false }); // down from the top: the box again
    expect(wheel({ deltaY: -90 })).toEqual({ reached: false, prevented: true }); // up at the top: into history
    expect(hooks.scrolled).toEqual([-2]); // 1173 px / 30 rows = 39 px a row
    el.scrollTop = 490;
    expect(wheel({ deltaY: 90 })).toEqual({ reached: false, prevented: true }); // down at the bottom: back toward the tail
    expect(hooks.scrolled).toEqual([-2, 2]);
    expect(wheel({ deltaY: 90 })).toEqual({ reached: false, prevented: false }); // at the tail of both: nothing to take
  });

  it("a slow trackpad adds up to whole rows", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    el.scrollTop = 0;
    for (let i = 0; i < 5; i += 1) wheel({ deltaY: -10 });
    expect(hooks.scrolled).toEqual([-1]);
  });

  it("scrolls sideways on a horizontal wheel and on shift+wheel, never into history", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    el.scrollTop = 0;
    expect(wheel({ deltaX: 40 })).toEqual({ reached: false, prevented: false }); // the browser scrolls the box sideways
    expect(wheel({ deltaY: -90, shiftKey: true })).toEqual({ reached: false, prevented: false });
    expect(hooks.scrolled).toEqual([]);
    // A diagonal swipe at the top: history, and the box moves sideways once. Both axes are applied
    // here and the event is cancelled, or the browser would add deltaX a second time.
    expect(wheel({ deltaX: 40, deltaY: -90 })).toEqual({ reached: false, prevented: true });
    expect(hooks.scrolled).toEqual([-2]);
    expect(el.scrollLeft).toBe(40);
  });

  it("cancels a diagonal wheel whose only movement is sideways, so it is not applied twice", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    el.scrollTop = 490;
    wheel({ deltaY: 4000 }); // the tail of the box and of history
    hooks.scrolled = [];
    expect(wheel({ deltaX: 40, deltaY: 90 })).toEqual({ reached: false, prevented: true });
    expect(el.scrollLeft).toBe(40);
  });

  it("reads the wheel's unit: pixels by the row height, lines as rows, pages as screens of rows", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1")); // 30 rows, 39 px each
    el.scrollTop = 0;
    wheel({ deltaY: -90, deltaMode: 0 });
    wheel({ deltaY: -3, deltaMode: 1 });
    wheel({ deltaY: -1, deltaMode: 2 });
    expect(hooks.scrolled).toEqual([-2, -3, -30]);
  });

  it("output that arrives while the viewer reads history does not move the view", async () => {
    const { el, emit } = await open();
    emit(screenEvent("s1"));
    el.scrollTop = 0; fireEvent.scroll(el);
    wheel({ deltaY: -400 });
    expect(hooks.term && (hooks.term as unknown as { buffer: { active: { viewportY: number } } }).buffer.active.viewportY).toBe(90);
    emit(screenEvent("s1", "output", 1));
    expect(el.scrollTop).toBe(0);
    expect(hooks.scrolled).toEqual([-10]); // nothing scrolled it back down
  });

  it("says so when a readable terminal is wider than the pane, and only then", async () => {
    const hint = "Wider than this pane · scroll sideways or choose Fit";
    const { emit, view } = await open();
    expect(screen.queryByText(hint)).toBeNull(); // nothing drawn yet
    emit(screenEvent("s1"));
    expect(screen.getByText(hint).closest(".session-terminal-scroll")).toBeNull(); // in the strip, not over the text
    fireEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(screen.queryByText(hint)).toBeNull();
    view.unmount();
    window.localStorage.clear();
    hooks.screen = { w: 600, h: 300 }; // an 80-column session fits a 660 px pane
    let send: (e: LiveEvent) => void = () => {};
    render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={(h) => { send = h; return () => {}; }} watch={async () => undefined} onLive={() => {}} />);
    await act(async () => { await vi.dynamicImportSettled(); });
    box();
    act(() => { send(screenEvent("s1")); });
    expect(screen.queryByText(hint)).toBeNull();
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

describe("a snapshot that arrives ahead of the output below it", () => {
  // Prod 2026-10-08, review of #3894: the Mac's output and the snapshot behind it are two requests
  // and can swap. Written at once, the snapshot covered the screen and the late output was dropped:
  // an inline Codex session's lines, scrolled off the top by that output, never reached scrollback.
  const mount = async () => {
    vi.useFakeTimers();
    let emit: (e: LiveEvent) => void = () => {};
    const view = render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }}
      onEvent={(h) => { emit = h; return () => {}; }} watch={vi.fn(async () => undefined)} onLive={vi.fn()} />);
    await act(async () => { await vi.dynamicImportSettled(); });
    const send = (kind: string, seq: number, text: string) => act(() => emit(screenEvent("s1", kind, seq, 1, btoa(text))));
    return { ...view, send };
  };

  it("draws a fresh viewer's first snapshot at once, whatever its seq", async () => {
    const { send } = await mount();
    send("snapshot", 7, "first");
    expect(hooks.did).toEqual(["reset", "first"]);
  });

  it("writes the output first in either arrival order, with no wait when it is in order", async () => {
    const { send } = await mount();
    send("snapshot", 0, "first");
    send("output", 1, "lines");
    send("snapshot", 2, "screen");
    expect(hooks.did).toEqual(["reset", "first", "lines", "screen"]);
    send("snapshot", 4, "after");
    expect(hooks.did).toHaveLength(4); // waits for seq 3
    send("output", 3, "more lines");
    expect(hooks.did.slice(4)).toEqual(["more lines", "after"]);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(hooks.did).toHaveLength(6); // the wait ended with the output: nothing is drawn again
  });

  it("draws the snapshot as a repair when the output never comes, and not after an unmount", async () => {
    const { send, unmount } = await mount();
    send("snapshot", 0, "first");
    send("snapshot", 2, "repair");
    await act(async () => { await vi.advanceTimersByTimeAsync(299); });
    expect(hooks.did).toEqual(["reset", "first"]);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(hooks.did).toEqual(["reset", "first", "repair"]);
    send("output", 1, "too late");
    send("snapshot", 5, "never drawn");
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(hooks.did).toEqual(["reset", "first", "repair"]);
  });
});
