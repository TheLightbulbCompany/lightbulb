"use client";

import { useEffect, useState } from "react";

type Invoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;

/** The desktop app's command bridge, absent in a browser. */
function bridge(): Invoke | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
}

/** True inside a desktop app that can open terminal sessions (older builds lack the command). */
export function useLocalTerminals(): boolean {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    const invoke = bridge();
    if (!invoke) return;
    let live = true;
    invoke("terminal_list").then(() => { if (live) setAvailable(true); }, () => {});
    return () => { live = false; };
  }, []);
  return available;
}

/** Asks the desktop app for a folder, then opens a terminal running the provider there. */
export function openLocalTerminal(provider: "claude" | "codex"): Promise<unknown> {
  return bridge()?.("terminal_open", { provider }) ?? Promise.resolve(null);
}
