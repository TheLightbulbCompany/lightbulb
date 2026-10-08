"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError, NEW_SCREEN, SNAPSHOT_WAIT_MS, receiveScreen, settleScreen, snapshotWaits, type LiveEvent, type LocalSession, type SessionScreen } from "@lightbulb/shared";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { createInputQueue, dropMouseReports, dropProbeReplies, SHIFT_ENTER } from "./sessionInput";

const WATCH_EVERY_MS = 15_000;
/** A snapshot or a ping comes at least every 10 s; this long with no frame, or only pings that do not match the screen it has, and the picture is frozen, so it says Reconnecting. */
const STALE_AFTER_MS = 30_000;
/** A viewer that lost frames asks the Mac for its lines again at most this often: each ask is a history burst out of the session's budget. */
const REPAIR_EVERY_MS = 30_000;
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
 *  go to the Mac with the screen epoch they were typed against. A refusal (`inputRefused`) stops input
 *  until the `session` prop changes; an offline Mac (409 LOCAL_SESSION_OFFLINE, transient)
 *  loses only that batch. The viewer never resizes the Mac's terminal:
 *  the grid is drawn at the Mac's own cols x rows and only scaled here (`terminalScale`), and where
 *  the scaled picture is larger than the box, the box scrolls. The wheel is this view's alone and
 *  never xterm's (`onWheel`): the box moves while it can, and only then the terminal's own
 *  scrollback. No click, drag or wheel is ever sent to the Mac (`dropMouseReports`). */
/** The session's own frames channel, `/ws/<ws>/session-frames/<sid>`: one AppSync publish per frame
 *  for every viewer, and AppSync admits only a current member while the session is shared. `claim`
 *  subscribes it on the one live socket and calls `onRefused` when AppSync refuses the subscribe. */
export type SessionFrames = {
  channel: string;
  claim: (channel: string, onRefused: () => void) => () => void;
  liveChannels: readonly string[];
};
const accessGone = (e: unknown) => e instanceof ApiError && (e.status === 403 || e.status === 404);

export function SessionTerminalView({ session, onEvent, watch, onLive, onCanType, input, admitted = false, kept = false, frames }: {
  session: Pick<LocalSession, "sessionId" | "generation">;
  /** Absent, frames are read off `onEvent` with no subscription of this view's own (tests). */
  frames?: SessionFrames;
  /** The session ended and its Mac reports the final screen. A view that watched it through the end asks
   *  for that picture once, as a new viewer does: until the report the backend refuses the watch and drops
   *  the Mac's frames, and a renewal after it is no new viewer, so nothing else sends the picture. */
  kept?: boolean;
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
  // Whether this view is asking the Mac for the screen: not while the tab is hidden (below).
  const [watching, setWatching] = useState(false);
  // No frame for STALE_AFTER_MS of watching: a Mac whose app predates terminal frames never sends
  // one. A hidden tab asked for nothing, so its silence says nothing about the Mac.
  const [silent, setSilent] = useState(false);
  useEffect(() => {
    if (seen || !watching) { setSilent(false); return; }
    const timer = setTimeout(() => setSilent(true), STALE_AFTER_MS);
    return () => clearTimeout(timer);
  }, [seen, watching, session.sessionId, session.generation]);
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<TerminalSize>(savedSize);
  const choose = (next: TerminalSize) => { setSize(next); try { window.localStorage.setItem(SIZE_KEY, next); } catch { /* storage blocked: this visit only */ } };
  // The drawn grid and the box around it, in px; null until both have a size (the box has none while unseen).
  const [seenAt, setSeenAt] = useState<{ grid: Extent; box: Extent } | null>(null);
  const scale = seenAt ? terminalScale(size, seenAt.grid, seenAt.box) : 1;
  // At the readable floor a wide session runs past the pane's right edge, and long lines end mid-word.
  const wide = size === "readable" && !!seenAt && Math.floor(seenAt.grid.w * scale) > seenAt.box.w;
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
  // The screen this view last asked for (a session's, or its kept one): `watch` may change identity
  // between renders, and each fresh ask costs every viewer a history burst.
  const asked = useRef("");
  const watchNow = useRef(watch);
  watchNow.current = watch;
  // The backend refused a new viewer: this session is full (LOCAL_SESSION_VIEWERS_FULL), with its message.
  const [full, setFull] = useState<string | null>(null);
  const screenKey = `${session.sessionId}:${kept}`;
  // The frames channel is held until access is lost (403/404 or a full session on a watch). A
  // refused subscribe is released and claimed again, 1 s doubling to 15 s, so the socket asks
  // anew; a refusal that lasts is a refused watch too, which ends it.
  const framesChannel = frames?.channel ?? null;
  const claim = frames?.claim;
  // The first watch asks the Mac for a picture: not before the channel it comes on is live.
  const subscribed = !frames || frames.liveChannels.includes(frames.channel);
  const [ready, setReady] = useState(subscribed);
  const [gone, setGone] = useState(false);
  const [claimRound, setClaimRound] = useState(0);
  const retryIn = useRef(1_000);
  const lost = useRef(() => {});
  lost.current = () => { setGone(true); setLive(false); onLive(false); };
  useEffect(() => {
    if (!framesChannel || !claim || gone) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const release = claim(framesChannel, () => {
      release();
      // A session shared before its marker existed gets one from a watch. Its picture goes out
      // now, unheard; the first real watch, once the retried subscription is live, asks again.
      void watchNow.current(session.sessionId, false).catch((e) => { if (accessGone(e)) lost.current(); });
      timer = setTimeout(() => setClaimRound((n) => n + 1), retryIn.current);
      retryIn.current = Math.min(retryIn.current * 2, 15_000);
    });
    return () => { clearTimeout(timer); release(); };
  }, [framesChannel, claim, gone, claimRound, session.sessionId]);
  useEffect(() => { if (subscribed) { setReady(true); retryIn.current = 1_000; } }, [subscribed]);
  useEffect(() => {
    if (!ready) return;
    let refused = false;
    setFull(null);
    const ping = (fresh: boolean) => void watch(session.sessionId, fresh).catch((e) => {
      if (!(e instanceof ApiError)) return;
      // The session already has its most live viewers: say so, and stop asking (no retry loop).
      const full = e.status === 409 && (e.body as { code?: string } | null)?.code === "LOCAL_SESSION_VIEWERS_FULL";
      if (full || accessGone(e)) { refused = true; clearInterval(timer); lost.current(); }
      if (full) setFull(e.message);
    });
    // A hidden tab does not watch. Its timers run about once a minute, past the 45 s viewer lease,
    // so each late renewal counted as a new viewer and cost the Mac a history burst. Hidden, the
    // lease just lapses; shown again, this view asks for one fresh screen and renews as before.
    let timer: ReturnType<typeof setInterval> | undefined;
    const start = (fresh: boolean) => {
      clearInterval(timer);
      ping(fresh);
      asked.current = screenKey;
      setWatching(true);
      timer = setInterval(() => { if (!refused) ping(false); }, WATCH_EVERY_MS);
    };
    const onVisibility = () => {
      if (document.visibilityState !== "hidden") { if (!refused) start(true); return; }
      clearInterval(timer); timer = undefined;
      setWatching(false);
    };
    if (document.visibilityState !== "hidden") start(asked.current !== screenKey);
    document.addEventListener("visibilitychange", onVisibility);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [session.sessionId, screenKey, watch, ready]);
  useEffect(() => {
    let term: Terminal | null = null;
    let order = NEW_SCREEN;
    let drawn = -1; // the relay run on screen; a new one starts the terminal over
    let wrote = ""; // the kind of the frame written last
    let sized = ""; // the size asked for last
    // The line, from the top, a reader who had scrolled up was on when the picture started over; -1: at the tail.
    // Kept while a redraw leaves them at its tail: the run's history can come after its snapshot, in parts
    // (`receiveScreen`), and each part starts the picture over again, so that redraw did not have their line yet.
    let readingAt = -1;
    const queued: SessionScreen[] = [];
    let alive = true;
    let stale: ReturnType<typeof setTimeout> | undefined;
    // Runs while a snapshot waits for the frames below it (`receiveScreen`); from the first one that waits.
    let settle: ReturnType<typeof setTimeout> | undefined;
    // A snapshot repaired the screen over frames that never came (`receiveScreen`): the lines they
    // scrolled off the top are missing above it. One fresh watch brings a history burst, which
    // starts this view's picture over with them; a view with nothing missing writes none of it.
    let repaired = 0;
    const repair = (lost?: boolean) => {
      if (!lost || document.visibilityState === "hidden" || Date.now() - repaired < REPAIR_EVERY_MS) return;
      repaired = Date.now();
      void watchNow.current(session.sessionId, true).catch(() => { /* the watch effect shows a refusal */ });
    };
    let pending = "";
    let frameId = 0;
    // One call at a time keeps keystrokes in order; a refusal drops what is queued behind it, at once,
    // and for as long as the session is the one refused: a restarted one (a new generation) gets a new queue.
    let queue: { key: string; send: ReturnType<typeof createInputQueue> } | null = null;
    const flush = () => {
      frameId = 0;
      const data = pending; pending = "";
      const now = typing.current;
      if (!data || !now.canType || !now.input) return;
      const { input, session: sent } = now;
      const epoch = order.epoch;
      const key = `${sent.sessionId}:${sent.generation}`;
      if (queue?.key !== key) queue = { key, send: createInputQueue(() => typing.current.refuse(key)) };
      queue.send(new TextEncoder().encode(data), (part) => typing.current.session.sessionId === sent.sessionId && typing.current.canType ? input(part, epoch) : undefined);
    };
    const fit = () => {
      const shown = host.current?.querySelector<HTMLElement>(".xterm-screen");
      if (!shown?.offsetWidth || !shown.offsetHeight || !around?.clientWidth || !around.clientHeight) return;
      const [gw, gh, bw, bh] = [shown.offsetWidth, shown.offsetHeight, around.clientWidth, around.clientHeight];
      setSeenAt((was) => was && was.grid.w === gw && was.grid.h === gh && was.box.w === bw && was.box.h === bh ? was : { grid: { w: gw, h: gh }, box: { w: bw, h: bh } });
      if (following.current) around.scrollTop = around.scrollHeight;
    };
    // The session's program (Claude Code) turns mouse tracking on, and xterm then takes every wheel
    // as a mouse report and cancels it (Terminal.ts bindMouse: its custom wheel handler cannot stop
    // that), or on a full-screen app sends arrow keys. So xterm never sees the wheel: stopped here,
    // in capture, above xterm's own element. Left alone, the browser scrolls the box, both ways.
    const around = box.current;
    let carry = 0; // a trackpad sends fractions of a row
    const onWheel = (e: WheelEvent) => {
      e.stopPropagation();
      const dy = e.shiftKey || e.ctrlKey ? 0 : e.deltaY; // shift+wheel is sideways, ctrl+wheel a zoom
      if (!around || !dy || (dy < 0 ? around.scrollTop > 0 : !atBottom(around))) return;
      // The box is at its end this way: the terminal's own scrollback takes over. From here both
      // axes are applied by hand, and the event is cancelled if either moved: left to the browser
      // as well, a diagonal swipe would move sideways twice.
      const shown = host.current?.querySelector<HTMLElement>(".xterm-screen");
      if (!term || !shown?.offsetHeight) return;
      // One row as drawn (the scaled height; jsdom has no layout, so the unscaled one there).
      const rowPx = (shown.getBoundingClientRect().height || shown.offsetHeight) / term.rows;
      // deltaMode: 0 is pixels, 1 lines, 2 pages (a page is the terminal's visible rows).
      const [rows, px] = e.deltaMode === 1 ? [1, rowPx] : e.deltaMode === 2 ? [term.rows, around.clientWidth] : [1 / rowPx, 1];
      const left = around.scrollLeft;
      around.scrollLeft += e.deltaX * px;
      carry += dy * rows;
      const lines = Math.trunc(carry);
      carry -= lines;
      const was = term.buffer.active.viewportY;
      if (lines) term.scrollLines(lines); // xterm keeps its place in history while output arrives, and follows again at the tail
      if (around.scrollLeft !== left || term.buffer.active.viewportY !== was) e.preventDefault();
    };
    around?.addEventListener("wheel", onWheel, { capture: true, passive: false });
    const apply = (frames: SessionScreen[]) => {
      if (!term) { queued.push(...frames); return; }
      let over = false; // the picture started over in this batch: its snapshot puts a reader back
      for (const frame of frames) {
        // History is only ever written as a burst under its snapshot, and the burst is the whole picture:
        // a run this view already draws without it (`receiveScreen`) starts over there as a new run does.
        const fresh = frame.epoch !== drawn || (frame.kind === "history" && wrote !== "history");
        const again = frame.epoch === drawn; // the same run, drawn again under history that came late
        const size = `${frame.cols}x${frame.rows}`;
        // xterm parses writes later, in order, but resets and resizes at once, and a reset leaves what is
        // queued in place (WriteBuffer, 5.5.0): done here, a reset would come before bytes written just
        // before it, and those bytes would then draw into the new picture (two late history chunks in one
        // tick: doubled scrollback). So both wait their turn behind an empty write.
        if (fresh || size !== sized) {
          const t = term;
          t.write("", () => {
            if (!alive) return;
            if (fresh) {
              // A reset goes to the tail. Someone reading further up stays on their line: a session that
              // ends, or a new relay run, draws the same lines again under them.
              // Where they are now, unless they still wait at the tail of this run's last redraw for their line.
              const at = t.buffer.active;
              if (!(again && readingAt >= 0 && at.viewportY >= at.baseY)) readingAt = at.viewportY < at.baseY ? at.viewportY : -1;
              t.reset();
            }
            if (t.cols !== frame.cols || t.rows !== frame.rows) t.resize(frame.cols, frame.rows);
          });
        }
        if (fresh) { drawn = frame.epoch; over = true; }
        wrote = frame.kind; sized = size;
        let bytes = decode(frame.data);
        if (frame.kind === "snapshot") {
          const redraw = new Uint8Array(CLEAR.length + bytes.length); redraw.set(CLEAR); redraw.set(bytes, CLEAR.length);
          bytes = redraw; setLive(true); setSeen(true); onLive(true);
        }
        const t = term;
        // Only the snapshot of a picture that started over: the 30 s resync moves nobody.
        const back = frame.kind === "snapshot" && over;
        if (back) over = false;
        term.write(bytes, !back ? fit : () => {
          if (alive && readingAt >= 0) {
            t.scrollToLine(readingAt);
            // On their line. Still at the tail: the picture is shorter than their line so far.
            if (t.buffer.active.viewportY < t.buffer.active.baseY) readingAt = -1;
          }
          fit();
        });
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
      // onBinary is deliberately not wired: it carries only X10 mouse reports.
      term.onData((d) => {
        const data = dropMouseReports(dropProbeReplies(d));
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
      repair(step.lost);
      if (!snapshotWaits(order)) { clearTimeout(settle); settle = undefined; }
      else settle ??= setTimeout(() => { settle = undefined; const late = settleScreen(order); order = late.order; apply(late.write); repair(late.lost); }, SNAPSHOT_WAIT_MS);
    });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    if (box.current) observer?.observe(box.current);
    return () => { alive = false; pending = ""; if (frameId) cancelAnimationFrame(frameId); termRef.current = null; clearTimeout(stale); clearTimeout(settle); off(); observer?.disconnect(); around?.removeEventListener("wheel", onWheel, { capture: true }); term?.dispose(); onLive(false); };
  }, [session.sessionId, session.generation, onEvent, onLive]);
  return <>
    <div className="session-terminal-view session-terminal-frame" data-live={live} role="region" aria-label={canType ? "Live terminal" : "Live terminal (view only)"}>
      <div className="session-terminal-size" role="group" aria-label="Terminal size">
        {wide && <span className="session-terminal-hint">Wider than this pane · scroll sideways or choose Fit</span>}
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
    {!live && <div className="session-terminal-wait" role="status">{full ?? (seen ? "Reconnecting…" : silent ? "Nothing has arrived from the Mac in 30 seconds. If its Lightbulb app is older than this page, update it to watch this session." : "Connecting…")}</div>}
  </>;
}
