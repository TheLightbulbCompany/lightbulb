"use client";

import { useEffect, useState, type RefObject } from "react";

type Invoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;
type Option = { id: string; label: string };
export type LocalMacOptions = {
  name?: string | null;
  sharing: boolean;
  folders: string[];
  cli?: { pathLine: string | null } | null;
  tools: { id: "claude" | "codex"; label: string; modeLabel: string; models: Option[]; modes: Option[]; efforts?: Option[]; defaultMode?: string; defaultEffort?: string | null }[];
};

/** The desktop app's command bridge, absent in a browser. */
export function bridge(): Invoke | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
}

/** Whether this app has `engine_ask`; the hosted page also runs in older desktop builds. An empty
 *  op is refused before the engine's socket, so the probe touches nothing; an older app says the
 *  command is not found instead. */
export async function hasEngineAsk(): Promise<boolean> {
  const invoke = bridge();
  if (!invoke) return false;
  return invoke("engine_ask", { op: "" }).then(() => true, (e) => !/command\b.*\bnot found|unknown command/i.test(String(e)));
}

/** This Mac's tools and approved folders, when the page runs inside a desktop app that opens
 *  terminal sessions itself; null in a browser and in older desktop builds. */
export function useLocalMac(): LocalMacOptions | null {
  const [options, setOptions] = useState<LocalMacOptions | null>(null);
  useEffect(() => {
    const invoke = bridge();
    if (!invoke) return;
    let live = true;
    invoke("terminal_options").then((o) => { if (live) setOptions(o as LocalMacOptions); }, () => {});
    return () => { live = false; };
  }, []);
  return options;
}

/** Start a held session on this Mac. It shows in this view at once and joins Sessions when it reports in. */
export async function startLocalTerminal(params: Record<string, unknown>): Promise<{ holderId: string }> {
  const invoke = bridge();
  if (!invoke) throw new Error("Open Lightbulb on your Mac to start a terminal here.");
  return (await invoke("terminal_start", { params })) as { holderId: string };
}

/** How many people watch this run, as the web terminal's `watch` answers it, while its terminal is
 *  on this Mac (`on`). The desktop app reads the presence its band draws: no request leaves the Mac. */
export function useLocalAudience(on: boolean, sessionId: string, generation: string): unknown {
  const key = `${sessionId}:${generation}`;
  const [counts, setCounts] = useState<{ key: string; value: unknown } | null>(null);
  useEffect(() => {
    const invoke = bridge();
    if (!on || !invoke) return;
    let live = true;
    const read = () => void invoke("local_session_audience", { sessionId, generation }).then((value) => { if (live) setCounts({ key, value }); }, () => {});
    read();
    // ponytail: a poll of an in-app read; an event from the app when presence changes, if 5 s ever shows.
    const timer = setInterval(read, 5000);
    return () => { live = false; clearInterval(timer); };
  }, [on, key, sessionId, generation]);
  return on && counts?.key === key ? counts.value : undefined;
}

export type LocalTerminal = "checking" | "local" | "none";
/** How often the panel's place is re-read: it moves when the content above it changes height. */
const TRACK_MS = 250;
/** While no holder answers: re-ask this often for the first REPROBE_FOR_MS, then every REPROBE_SLOW_MS
 *  until the session ends or the panel unmounts. */
const REPROBE_MS = 2000;
const REPROBE_FOR_MS = 30_000;
const REPROBE_SLOW_MS = 10_000;
const LIVE_PROBE_MS = 5000;
// Strictly increasing: the backend drops a non-greater `at`, and a close and the next open can share a clock tick.
let last = 0;
const now = () => (last = Math.max(performance.timeOrigin + performance.now(), last + 0.01));

/**
 * The session's live terminal on this Mac, drawn by the desktop app over `slot` (its own webview;
 * this page never touches the terminal or its key). "local": it is there. "none": not in the
 * desktop app, or this Mac does not run the session. `visible` false (an overlay is open) and
 * unmounting take it away; neither ends the session. So does a modal dialog (`ui/dialog`,
 * `ui/alert-dialog`: their overlay carries `data-modal-overlay`): the terminal is drawn above the
 * whole page, and a confirmation under it could not be seen or answered. A popover or the emoji
 * picker is not modal and leaves the terminal where it is.
 */
export function useLocalTerminal(target: { sessionId: string } | { holderId: string } | null,
  slot: RefObject<HTMLElement | null>, visible: boolean): LocalTerminal {
  const invoke = bridge();
  const key = target ? ("sessionId" in target ? `s:${target.sessionId}` : `h:${target.holderId}`) : null;
  const [result, setResult] = useState<{ key: string; value: LocalTerminal } | null>(null);
  useEffect(() => {
    if (!invoke || !target || !key) return;
    let alive = true;
    let sent = "";
    let none = false; // the last answer was "this Mac does not run it (yet)"
    let probedAt = 0;
    const t0 = Date.now();
    const report = () => {
      const box = slot.current?.getBoundingClientRect();
      const rect = visible && !document.querySelector("[data-modal-overlay]") && box && box.width > 0 && box.height > 0 ? { x: box.left, y: box.top, width: box.width, height: box.height } : null;
      // The page's own size too: the app places the terminal in the window, and the page can start below the window's top.
      const viewport = rect ? { width: innerWidth, height: innerHeight } : null;
      const shape = JSON.stringify([rect, viewport]);
      // The holder can be found after the panel opens (the session can register before its holder
      // is indexed): keep asking while it runs. The slot is unmounted while the answer is "none", so
      // the probe carries no rect; a "live" answer mounts the slot and the next tick places it.
      const gap = Date.now() - t0 < REPROBE_FOR_MS ? REPROBE_MS : REPROBE_SLOW_MS;
      // A successful placement is not a lifetime guarantee. The process can exit
      // without another layout change (including before its first prompt).
      const reprobe = Date.now() - probedAt >= (none ? gap : LIVE_PROBE_MS);
      if (shape === sent && !reprobe) return;
      sent = shape;
      probedAt = Date.now();
      invoke("terminal_panel", { ...target, rect, ...(viewport ? { viewport } : {}), at: now() }).then(
        (reply) => {
          const r = reply as { live?: boolean; stale?: boolean };
          if (r?.stale) { sent = ""; return; } // lost a race: re-send on the next tick
          none = !r?.live;
          if (alive) setResult({ key, value: r?.live ? "local" : "none" });
        },
        () => { sent = ""; if (alive) setResult({ key, value: "none" }); });
    };
    report();
    const timer = setInterval(report, TRACK_MS);
    addEventListener("resize", report);
    return () => {
      alive = false; clearInterval(timer); removeEventListener("resize", report);
      void invoke("terminal_panel", { ...target, rect: null, at: now() }).catch(() => {});
    };
    // `key` names the target; `target` itself is a fresh object every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invoke, key, visible, slot]);
  if (!invoke || !target) return "none";
  return result?.key === key ? result.value : "checking";
}
