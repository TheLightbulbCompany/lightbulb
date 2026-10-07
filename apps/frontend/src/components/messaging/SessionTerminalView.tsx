"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError, NEW_SCREEN, receiveScreen, type LiveEvent, type LocalSession, type SessionScreen } from "@lightbulb/shared";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { chunkInput, dropProbeReplies, SHIFT_ENTER } from "./sessionInput";

const WATCH_EVERY_MS = 15_000;
/** A snapshot or a ping comes at least every 10 s; this long with no frame, or only pings that do not match the screen it has, and the picture is frozen, so it says Reconnecting. */
const STALE_AFTER_MS = 30_000;
/** Stops input: no access (403/404) or a Mac that cannot take typing. OFFLINE is another 409 and passes. */
const refuses = (e: unknown) => e instanceof ApiError && (e.status === 403 || e.status === 404
  || (e.status === 409 && (e.body as { code?: string } | null)?.code === "LOCAL_SESSION_UNSUPPORTED"));
const decode = (data: string) => Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
/** Clears the visible screen only: xterm's erase never pushes it into scrollback, so history stays.
 *  First it leaves the alternate screen and drops the scroll region, so modes the last screen set
 *  do not outlive it; the holder's snapshot enters the alternate screen itself when it is on one. */
const CLEAR = new TextEncoder().encode("\x1b[?1049l\x1b[r\x1b[2J\x1b[H");

/** The smallest the terminal is drawn in Readable: 13 px text at 0.75 is about 10 px. Below that
 *  the box scrolls instead (a 124-column session in a 660 px pane once drew at 0.38, about 5 px). */
const MIN_SCALE = 0.75;
export type TerminalSize = "readable" | "fit";
type Extent = { w: number; h: number };
/** Readable fits the width only and holds the floor; Fit shows the whole screen, however small. */
export const terminalScale = (size: TerminalSize, grid: Extent, box: Extent): number =>
  size === "fit" ? Math.min(1, box.w / grid.w, box.h / grid.h) : Math.min(1, Math.max(MIN_SCALE, box.w / grid.w));
const SIZE_KEY = "lightbulb:session-terminal-size";
const savedSize = (): TerminalSize => { try { return window.localStorage.getItem(SIZE_KEY) === "fit" ? "fit" : "readable"; } catch { return "readable"; } };
/** Whether the box shows its last row (1 px of slack for fractional scales). */
const atBottom = (el: HTMLElement) => el.scrollTop + el.clientHeight >= el.scrollHeight - 1;

/** A running session's real terminal, live (rulings 2026-10-03 §4): for teammates, and for its
 *  owner anywhere but at that Mac. Read-only unless `input` is given and the person is `admitted`
 *  (owner, allowed sender or team control, re-derived by the caller each render); then keystrokes
 *  go to the Mac with the screen epoch they were typed against. A refusal (`refuses`) stops input
 *  until the `session` prop changes; an offline Mac (409 LOCAL_SESSION_OFFLINE, transient)
 *  loses only that batch. The viewer never resizes the Mac's terminal:
 *  the grid is drawn at the Mac's own cols x rows and only scaled here (`terminalScale`), and where
 *  the scaled picture is larger than the box, the box scrolls. One wheel rule covers both scrolls:
 *  the box moves while it can, and only then the terminal's own scrollback. */
export function SessionTerminalView({ session, onEvent, watch, onLive, onCanType, input, admitted = false }: {
  session: Pick<LocalSession, "sessionId" | "generation">;
  input?: (data: Uint8Array, epoch: number) => Promise<unknown>;
  admitted?: boolean;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  /** `fresh`: this view just mounted and has no screen, so the Mac is asked for one now. */
  watch: (sessionId: string, fresh?: boolean) => Promise<unknown>;
  onLive: (live: boolean) => void;
  /** Whether this viewer can type right now; false once the Mac refuses input. */
  onCanType?: (canType: boolean) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [live, setLive] = useState(false);
  // Whether a frame ever arrived: "Connecting…" until then, "Reconnecting…" after the picture went stale.
  const [seen, setSeen] = useState(false);
  // No frame for STALE_AFTER_MS after mount: a Mac whose app predates terminal frames never sends one.
  const [silent, setSilent] = useState(false);
  useEffect(() => {
    if (seen) { setSilent(false); return; }
    const timer = setTimeout(() => setSilent(true), STALE_AFTER_MS);
    return () => clearTimeout(timer);
  }, [seen, session.sessionId, session.generation]);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<TerminalSize>(savedSize);
  const choose = (next: TerminalSize) => { setSize(next); try { window.localStorage.setItem(SIZE_KEY, next); } catch { /* storage blocked: this visit only */ } };
  // The drawn grid and the box around it, in px; null until both have a size (the box has none while unseen).
  const [seenAt, setSeenAt] = useState<{ grid: Extent; box: Extent } | null>(null);
  const scale = seenAt ? terminalScale(size, seenAt.grid, seenAt.box) : 1;
  // The prompt is on the last row: stay there until the viewer scrolls away, and again once they come back.
  const following = useRef(true);
  useLayoutEffect(() => { if (following.current && box.current) box.current.scrollTop = box.current.scrollHeight; }, [scale, seenAt]);
  const [refusedFor, setRefusedFor] = useState<string | null>(null);
  // Keyed by id and generation, not the object: the session list swaps the object on every refresh.
  const key = `${session.sessionId}:${session.generation}`;
  const canType = !!input && admitted && refusedFor !== key;
  useEffect(() => { onCanType?.(canType); }, [canType, onCanType]);
  const typing = useRef({ canType, input, session, refuse: (k: string) => setRefusedFor(k) });
  typing.current = { canType, input, session, refuse: (k: string) => setRefusedFor(k) };
  const termRef = useRef<Terminal | null>(null);
  useEffect(() => { if (termRef.current) termRef.current.options.disableStdin = !canType; }, [canType]);
  // The session this view last asked a screen for: `watch` may change identity between renders,
  // and each fresh ask costs every viewer a history burst.
  const asked = useRef("");
  useEffect(() => {
    let refused = false;
    const ping = (fresh: boolean) => void watch(session.sessionId, fresh).catch((e) => { if (e instanceof ApiError && (e.status === 403 || e.status === 404)) { refused = true; setLive(false); onLive(false); } });
    ping(asked.current !== session.sessionId);
    asked.current = session.sessionId;
    const timer = setInterval(() => { if (!refused) ping(false); }, WATCH_EVERY_MS);
    return () => clearInterval(timer);
  }, [session.sessionId, watch, onLive]);
  useEffect(() => {
    let term: Terminal | null = null;
    let order = NEW_SCREEN;
    let drawn = -1; // the relay run on screen; a new one starts the terminal over
    const queued: SessionScreen[] = [];
    let alive = true;
    let stale: ReturnType<typeof setTimeout> | undefined;
    let pending = "";
    let frameId = 0;
    let sending: Promise<unknown> = Promise.resolve();
    const flush = () => {
      frameId = 0;
      const data = pending; pending = "";
      const now = typing.current;
      if (!data || !now.canType || !now.input) return;
      const { input, session: sent } = now;
      const epoch = order.epoch;
      for (const part of chunkInput(new TextEncoder().encode(data))) {
        // One call at a time keeps keystrokes in order; a refusal drops what is queued behind it.
        sending = sending.then(() => typing.current.session.sessionId === sent.sessionId && typing.current.canType ? input(part, epoch) : undefined).catch((e) => {
          if (refuses(e)) typing.current.refuse(`${sent.sessionId}:${sent.generation}`);
        });
      }
    };
    const fit = () => {
      const shown = host.current?.querySelector<HTMLElement>(".xterm-screen");
      const around = box.current;
      if (!shown?.offsetWidth || !shown.offsetHeight || !around?.clientWidth || !around.clientHeight) return;
      const [gw, gh, bw, bh] = [shown.offsetWidth, shown.offsetHeight, around.clientWidth, around.clientHeight];
      setSeenAt((was) => was && was.grid.w === gw && was.grid.h === gh && was.box.w === bw && was.box.h === bh ? was : { grid: { w: gw, h: gh }, box: { w: bw, h: bh } });
      if (following.current) around.scrollTop = around.scrollHeight;
    };
    const apply = (frames: SessionScreen[]) => {
      if (!term) { queued.push(...frames); return; }
      for (const frame of frames) {
        if (frame.epoch !== drawn) { term.reset(); drawn = frame.epoch; }
        if (term.cols !== frame.cols || term.rows !== frame.rows) term.resize(frame.cols, frame.rows);
        let bytes = decode(frame.data);
        if (frame.kind === "snapshot") {
          const redraw = new Uint8Array(CLEAR.length + bytes.length); redraw.set(CLEAR); redraw.set(bytes, CLEAR.length);
          bytes = redraw; setLive(true); setSeen(true); onLive(true);
        }
        term.write(bytes, fit);
      }
    };
    void import("@xterm/xterm").then(({ Terminal }) => {
      if (!alive || !host.current) return;
      // xterm takes colour strings, not var(): read the terminal tokens from globals.css.
      const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || undefined;
      term = new Terminal({ disableStdin: true, cursorBlink: false, scrollback: 5000, fontSize: 13,
        fontFamily: "Menlo, 'SF Mono', monospace", theme: { background: token("--terminal-bg"), foreground: token("--terminal-fg") } });
      term.open(host.current);
      termRef.current = term;
      term.options.disableStdin = !typing.current.canType;
      term.attachCustomKeyEventHandler((e) => {
        if (e.type === "keydown" && e.key === "Enter" && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
          if (typing.current.canType) { pending += SHIFT_ENTER; frameId ||= requestAnimationFrame(flush); }
          return false;
        }
        return true;
      });
      // xterm scrolls its scrollback on the wheel (and on a full-screen app sends arrow keys) and
      // would never let the box move. `false` leaves the event to the browser, which scrolls the box.
      term.attachCustomWheelEventHandler((e) => {
        const around = box.current;
        return !around || !(e.deltaY < 0 ? around.scrollTop > 0 : e.deltaY > 0 && !atBottom(around));
      });
      term.onData((d) => {
        const data = dropProbeReplies(d);
        if (!data || !typing.current.canType) return;
        pending += data; frameId ||= requestAnimationFrame(flush);
      });
      apply(queued.splice(0));
    });
    const off = onEvent((event) => {
      if (event.type !== "workspace.session.screen") return;
      const frame = event.payload;
      if (frame.sessionId !== session.sessionId || frame.generation !== session.generation) return;
      // A ping proves the picture is current only when it names the frame this view waits for:
      // one ahead of that means a frame was lost, and one with no screen yet proves nothing.
      const current = frame.kind === "ping" && order.synced && frame.epoch === order.epoch && frame.seq === order.next;
      if (frame.kind !== "ping" || current) {
        clearTimeout(stale);
        stale = setTimeout(() => { setLive(false); onLive(false); }, STALE_AFTER_MS);
      }
      if (current) { setLive(true); onLive(true); }
      const step = receiveScreen(order, frame);
      order = step.order;
      apply(step.write);
    });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    if (box.current) observer?.observe(box.current);
    return () => { alive = false; pending = ""; if (frameId) cancelAnimationFrame(frameId); termRef.current = null; clearTimeout(stale); off(); observer?.disconnect(); term?.dispose(); onLive(false); };
  }, [session.sessionId, session.generation, onEvent, onLive]);
  return <>
    <div className="session-terminal-view session-terminal-frame" data-live={live} role="region" aria-label={canType ? "Live terminal" : "Live terminal (view only)"}>
      <div className="session-terminal-size" role="group" aria-label="Terminal size">
        <button type="button" aria-pressed={size === "readable"} title="Text at a readable size; scroll to see the rest" onClick={() => choose("readable")}>Readable</button>
        <button type="button" aria-pressed={size === "fit"} title="The whole screen at once" onClick={() => choose("fit")}>Fit</button>
      </div>
      <div ref={box} className="session-terminal-scroll" onScroll={(e) => { following.current = atBottom(e.currentTarget); }}>
        {/* The wrapper has the scaled size: a transform leaves the layout size alone, so the box would scroll past the picture.
            Rounded down, so Fit never overflows the box by a fraction of a pixel and grows a scrollbar. */}
        <div style={seenAt ? { width: Math.floor(seenAt.grid.w * scale), height: Math.floor(seenAt.grid.h * scale), overflow: "hidden" } : undefined}>
          <div ref={host} style={{ width: seenAt?.grid.w, height: seenAt?.grid.h, transform: `scale(${scale})`, transformOrigin: "top left" }} />
        </div>
      </div>
    </div>
    {!live && <div className="session-terminal-wait" role="status">{seen ? "Reconnecting…" : silent ? "Nothing has arrived from the Mac in 30 seconds. If its Lightbulb app is older than this page, update it to watch this session." : "Connecting…"}</div>}
  </>;
}
