"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, NEW_SCREEN, receiveScreen, type LiveEvent, type LocalSession, type SessionScreen } from "@lightbulb/shared";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { chunkInput, dropProbeReplies, SHIFT_ENTER } from "./sessionInput";

const WATCH_EVERY_MS = 15_000;
/** A snapshot or a ping comes at least every 10 s; this long with neither and the picture is frozen, so it says Reconnecting. */
const STALE_AFTER_MS = 30_000;
/** Stops input: no access (403/404) or a Mac that cannot take typing. OFFLINE is another 409 and passes. */
const refuses = (e: unknown) => e instanceof ApiError && (e.status === 403 || e.status === 404
  || (e.status === 409 && (e.body as { code?: string } | null)?.code === "LOCAL_SESSION_UNSUPPORTED"));
const decode = (data: string) => Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
/** Clears the visible screen only: xterm's erase never pushes it into scrollback, so history stays. */
const CLEAR = new TextEncoder().encode("\x1b[2J\x1b[H");

/** A running session's real terminal, live (rulings 2026-10-03 §4): for teammates, and for its
 *  owner anywhere but at that Mac. Read-only unless `input` is given and the person is `admitted`
 *  (owner, allowed sender or team control, re-derived by the caller each render); then keystrokes
 *  go to the Mac with the screen epoch they were typed against. A refusal (`refuses`) stops input
 *  until the `session` prop changes; an offline Mac (409 LOCAL_SESSION_OFFLINE, transient)
 *  loses only that batch. The viewer never resizes the Mac's terminal. */
export function SessionTerminalView({ session, onEvent, watch, onLive, onCanType, input, admitted = false }: {
  session: Pick<LocalSession, "sessionId" | "generation">;
  input?: (data: Uint8Array, epoch: number) => Promise<unknown>;
  admitted?: boolean;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  watch: (sessionId: string) => Promise<unknown>;
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
  const [scale, setScale] = useState(1);
  const [refusedFor, setRefusedFor] = useState<string | null>(null);
  // Keyed by id and generation, not the object: the session list swaps the object on every refresh.
  const key = `${session.sessionId}:${session.generation}`;
  const canType = !!input && admitted && refusedFor !== key;
  useEffect(() => { onCanType?.(canType); }, [canType, onCanType]);
  const typing = useRef({ canType, input, session, refuse: (k: string) => setRefusedFor(k) });
  typing.current = { canType, input, session, refuse: (k: string) => setRefusedFor(k) };
  const termRef = useRef<Terminal | null>(null);
  useEffect(() => { if (termRef.current) termRef.current.options.disableStdin = !canType; }, [canType]);
  useEffect(() => {
    let refused = false;
    const ping = () => void watch(session.sessionId).catch((e) => { if (e instanceof ApiError && (e.status === 403 || e.status === 404)) { refused = true; setLive(false); onLive(false); } });
    ping();
    const timer = setInterval(() => { if (!refused) ping(); }, WATCH_EVERY_MS);
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
      const box = host.current?.parentElement;
      if (shown?.offsetWidth && box) setScale(Math.min(1, box.clientWidth / shown.offsetWidth, box.clientHeight / shown.offsetHeight));
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
      clearTimeout(stale);
      stale = setTimeout(() => { setLive(false); onLive(false); }, STALE_AFTER_MS);
      const step = receiveScreen(order, frame);
      order = step.order;
      apply(step.write);
    });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    if (host.current?.parentElement) observer?.observe(host.current.parentElement);
    return () => { alive = false; pending = ""; if (frameId) cancelAnimationFrame(frameId); termRef.current = null; clearTimeout(stale); off(); observer?.disconnect(); term?.dispose(); onLive(false); };
  }, [session.sessionId, session.generation, onEvent, onLive]);
  return <>
    <div className="session-terminal-view" data-live={live} role="region" aria-label={canType ? "Live terminal" : "Live terminal (view only)"}>
      <div ref={host} style={{ transform: `scale(${scale})`, transformOrigin: "top left" }} />
    </div>
    {!live && <div className="session-terminal-wait" role="status">{seen ? "Reconnecting…" : silent ? "Nothing has arrived from the Mac in 30 seconds. If its Lightbulb app is older than this page, update it to watch this session." : "Connecting…"}</div>}
  </>;
}
