"use client";

import { useEffect, useState } from "react";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
type AgentChromeState = "ready" | "stopped" | "chrome_missing" | "browser_off";

const LINES: Partial<Record<AgentChromeState, string>> = {
  ready: "Agents use a separate 'Lightbulb agent' Chrome window — ready",
  stopped: "Agents use a separate 'Lightbulb agent' Chrome window — starts when an agent needs it",
  chrome_missing: "Install Google Chrome to let agents browse on this Mac",
};

/** One line under the Browser switch: the agent Chrome's status, from the Mac app's
 *  `agent_chrome_status`. Nothing when Browser is off, and nothing on an older app. */
export function LocalBrowserSetup() {
  const [state, setState] = useState<AgentChromeState | null>(null);
  useEffect(() => {
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    invoke<{ state?: AgentChromeState } | null | undefined>("agent_chrome_status").then(
      status => setState(status?.state ?? null),
      () => setState(null),
    );
  }, []);
  const line = state ? LINES[state] : undefined;
  if (!line) return null;
  return <p className="settings-row-note" role="status">{line}</p>;
}
