"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, type LiveEvent, type LocalSession, type SessionScreen } from "@lightbulb/shared";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { NEW_SCREEN, receiveScreen } from "./sessionScreen";

const WATCH_EVERY_MS = 15_000;
/** Snapshots come every 10 s; this long with no frame at all and the picture is frozen, so the transcript takes over again. */
const STALE_AFTER_MS = 30_000;
const decode = (data: string) => Uint8Array.from(atob(data), (c) => c.charCodeAt(0));

/** A running session's real terminal, live and read-only (rulings 2026-10-03 §4): for teammates,
 *  and for its owner anywhere but at that Mac. Nothing typed here goes anywhere; the message box
 *  under it is how a person steers. Hidden (but laid out) until the first snapshot arrives. */
export function SessionTerminalView({ session, onEvent, watch, onLive }: {
  session: Pick<LocalSession, "sessionId" | "generation">;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  watch: (sessionId: string) => Promise<unknown>;
  onLive: (live: boolean) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [live, setLive] = useState(false);
  const [scale, setScale] = useState(1);
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
    const queued: SessionScreen[] = [];
    let alive = true;
    let stale: ReturnType<typeof setTimeout> | undefined;
    const fit = () => {
      const drawn = host.current?.querySelector<HTMLElement>(".xterm-screen");
      const box = host.current?.parentElement;
      if (drawn?.offsetWidth && box) setScale(Math.min(1, box.clientWidth / drawn.offsetWidth, box.clientHeight / drawn.offsetHeight));
    };
    const apply = (frames: SessionScreen[]) => {
      if (!term) { queued.push(...frames); return; }
      for (const frame of frames) {
        if (frame.kind === "snapshot") { term.reset(); setLive(true); onLive(true); }
        if (term.cols !== frame.cols || term.rows !== frame.rows) term.resize(frame.cols, frame.rows);
        term.write(decode(frame.data), fit);
      }
    };
    void import("@xterm/xterm").then(({ Terminal }) => {
      if (!alive || !host.current) return;
      term = new Terminal({ disableStdin: true, cursorBlink: false, scrollback: 0, fontSize: 13,
        fontFamily: "Menlo, 'SF Mono', monospace", theme: { background: "#16161a", foreground: "#e6e6e9" } });
      term.open(host.current);
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
    return () => { alive = false; clearTimeout(stale); off(); observer?.disconnect(); term?.dispose(); onLive(false); };
  }, [session.sessionId, session.generation, onEvent, onLive]);
  return <div className="session-terminal-view" data-live={live} role="region" aria-label="Live terminal (view only)">
    <div ref={host} style={{ transform: `scale(${scale})`, transformOrigin: "top left" }} />
  </div>;
}
