"use client";

import { useEffect } from "react";
import { toast } from "sonner";

export const EXPLICIT_SHARING_SEEN_KEY = "lightbulb.desktop.explicitSharingNotice";

type Invoke = <T>(command: string) => Promise<T>;

/** Once per Mac, inside the desktop app, where session sharing is on: plain `claude` and `codex`
 *  sessions stopped being shared (managed session sharing, spec §8). */
export function ExplicitSharingNotice() {
  useEffect(() => {
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    try { if (localStorage.getItem(EXPLICIT_SHARING_SEEN_KEY)) return; } catch { return; }
    let live = true;
    invoke<{ enrollment: { enabled: boolean } | null }>("local_sessions_status").then((status) => {
      if (!live || !status.enrollment?.enabled) return;
      try { localStorage.setItem(EXPLICIT_SHARING_SEEN_KEY, "1"); } catch { /* storage off: shown again next launch */ }
      toast(<span>Sessions are now shared only when you start them in Lightbulb or with <code>lightbulb claude</code>.</span>,
        { duration: Infinity });
    }, () => {});
    return () => { live = false; };
  }, []);
  return null;
}
