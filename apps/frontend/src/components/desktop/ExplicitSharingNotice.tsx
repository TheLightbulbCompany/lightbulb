"use client";

import { useEffect } from "react";
import { toast } from "sonner";

export const EXPLICIT_SHARING_SEEN_KEY = "lightbulb.desktop.allSessionsNotice";

type Invoke = <T>(command: string) => Promise<T>;

/** Once per Mac, inside the desktop app, where session sharing is on: every Claude Code and Codex
 *  session is shared again, plain `claude` and `codex` included (rulings 2026-10-04). */
export function ExplicitSharingNotice() {
  useEffect(() => {
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    try { if (localStorage.getItem(EXPLICIT_SHARING_SEEN_KEY)) return; } catch { return; }
    let live = true;
    invoke<{ enrollment: { enabled: boolean } | null }>("local_sessions_status").then((status) => {
      if (!live || !status.enrollment?.enabled) return;
      try { localStorage.setItem(EXPLICIT_SHARING_SEEN_KEY, "1"); } catch { /* storage off: shown again next launch */ }
      toast(<span>Every Claude Code and Codex session on this Mac is shared with your workspace again, including plain <code>claude</code> and <code>codex</code>. Pause one, or turn sharing off in Settings.</span>,
        { duration: Infinity });
    }, () => {});
    return () => { live = false; };
  }, []);
  return null;
}
