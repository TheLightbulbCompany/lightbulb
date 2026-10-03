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
function bridge(): Invoke | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
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

export type LocalTerminal = "checking" | "local" | "none";
/** How often the panel's place is re-read: it moves when the content above it changes height. */
const TRACK_MS = 250;
// Strictly increasing: the backend drops a non-greater `at`, and a close and the next open can share a clock tick.
let last = 0;
const now = () => (last = Math.max(performance.timeOrigin + performance.now(), last + 0.01));

/**
 * The session's live terminal on this Mac, drawn by the desktop app over `slot` (its own webview;
 * this page never touches the terminal or its key). "local": it is there. "none": not in the
 * desktop app, or this Mac does not run the session. `visible` false (an overlay is open) and
 * unmounting take it away; neither ends the session.
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
    const report = () => {
      const box = slot.current?.getBoundingClientRect();
      const rect = visible && box && box.width > 0 && box.height > 0 ? { x: box.left, y: box.top, width: box.width, height: box.height } : null;
      const shape = JSON.stringify(rect);
      if (shape === sent) return;
      sent = shape;
      invoke("terminal_panel", { ...target, rect, at: now() }).then(
        (reply) => {
          const r = reply as { live?: boolean; stale?: boolean };
          if (r?.stale) { sent = ""; return; } // lost a race: re-send on the next tick
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
