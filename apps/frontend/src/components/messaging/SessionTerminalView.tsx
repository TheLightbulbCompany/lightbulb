"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, type LiveEvent, type LocalSession, type SessionScreen } from "@lightbulb/shared";
import type { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { NEW_SCREEN, receiveScreen } from "./sessionScreen";
import { chunkInput, dropProbeReplies, SHIFT_ENTER } from "./sessionInput";

const WATCH_EVERY_MS = 15_000;
/** Snapshots come every 10 s; this long with no frame at all and the picture is frozen, so the transcript takes over again. */
const STALE_AFTER_MS = 30_000;
/** Stops input: no access (403/404) or a Mac that cannot take typing. OFFLINE is another 409 and passes. */
const refuses = (e: unknown) => e instanceof ApiError && (e.status === 403 || e.status === 404
  || (e.status === 409 && (e.body as { code?: string } | null)?.code === "LOCAL_SESSION_UNSUPPORTED"));
const decode = (data: string) => Uint8Array.from(atob(data), (c) => c.charCodeAt(0));

/** A running session's real terminal, live (rulings 2026-10-03 §4): for teammates, and for its
 *  owner anywhere but at that Mac. Read-only unless `input` is given and the person is `admitted`
 *  (owner, allowed sender or team control, re-derived by the caller each render); then keystrokes
 *  go to the Mac with the screen epoch they were typed against. A refusal (`refuses`) stops input
 *  until the `session` prop changes; an offline Mac (409 LOCAL_SESSION_OFFLINE, transient)
 *  loses only that batch. The viewer never resizes the Mac's terminal. */
export function SessionTerminalView({ session, onEvent, watch, onLive, input, admitted = false }: {
  session: Pick<LocalSession, "sessionId" | "generation">;
  input?: (data: Uint8Array, epoch: number) => Promise<unknown>;
  admitted?: boolean;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  watch: (sessionId: string) => Promise<unknown>;
  onLive: (live: boolean) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [live, setLive] = useState(false);
  const [scale, setScale] = useState(1);
  const [refusedFor, setRefusedFor] = useState<unknown>(null);
  const canType = !!input && admitted && refusedFor !== session;
  const typing = useRef({ canType, input, session, refuse: (s: unknown) => setRefusedFor(s) });
  typing.current = { canType, input, session, refuse: (s: unknown) => setRefusedFor(s) };
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
          if (refuses(e)) typing.current.refuse(sent);
        });
      }
    };
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
  return <div className="session-terminal-view" data-live={live} role="region" aria-label={canType ? "Live terminal" : "Live terminal (view only)"}>
    <div ref={host} style={{ transform: `scale(${scale})`, transformOrigin: "top left" }} />
  </div>;
}
