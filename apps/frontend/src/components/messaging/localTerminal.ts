"use client";

import { useEffect, useState } from "react";

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

/** Start a session in a terminal window on this Mac. `linked`: it will report in under Sessions. */
export async function startLocalTerminal(params: Record<string, unknown>): Promise<{ linked: boolean }> {
  const invoke = bridge();
  if (!invoke) throw new Error("Open Lightbulb on your Mac to start a terminal here.");
  return (await invoke("terminal_start", { params })) as { linked: boolean };
}
