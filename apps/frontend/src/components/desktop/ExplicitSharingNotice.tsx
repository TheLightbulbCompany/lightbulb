"use client";

import { useEffect } from "react";
import { toast } from "sonner";

// v2: the notice names Cursor and Grok CLI, which Share every new session now shares too; anyone
// who dismissed the Claude-and-Codex wording sees it once more.
export const EXPLICIT_SHARING_SEEN_KEY = "lightbulb.desktop.allSessionsNotice.v2";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

/** Once per Mac, inside the desktop app, where session sharing is on: which sessions are shared
 *  (owner ruling 2026-10-08: one it was started with Lightbulb, Share to Lightbulb, or the setting). */
export function ExplicitSharingNotice() {
  useEffect(() => {
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    try { if (localStorage.getItem(EXPLICIT_SHARING_SEEN_KEY)) return; } catch { return; }
    let live = true;
    invoke<{ enrollment: { enabled: boolean } | null }>("local_sessions_status").then((status) => {
      if (!live || !status.enrollment?.enabled) return;
      try { localStorage.setItem(EXPLICIT_SHARING_SEEN_KEY, "1"); } catch { /* storage off: shown again next launch */ }
      toast(<span>Every new Claude Code, Codex, Cursor or Grok CLI session on this Mac is shared with your workspace. Set one to Only me (Stop sharing in its terminal), or turn sharing off in Settings.</span>,
        { duration: Infinity });
    }, () => {});
    return () => { live = false; };
  }, []);
  return null;
}

export const LIVE_SHARING_OFFER_KEY = "lightbulb.desktop.liveSharingOffer";

/** Once per Mac, inside the desktop app, where session sharing is on and "Share every session
 *  live" is off: the offer that pairing makes (DesktopPermissionsGate), for a Mac that was paired
 *  before the switch existed. Nothing on an app build without the command. */
export function LiveSharingOffer() {
  useEffect(() => {
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    try { if (localStorage.getItem(LIVE_SHARING_OFFER_KEY)) return; } catch { return; }
    let live = true;
    Promise.all([
      invoke<{ enrollment: { enabled: boolean } | null }>("local_sessions_status"),
      invoke<boolean>("shell_integration_status"),
    ]).then(([status, on]) => {
      if (!live || !status.enrollment?.enabled || on !== false) return;
      try { localStorage.setItem(LIVE_SHARING_OFFER_KEY, "1"); } catch { /* storage off: offered again next launch */ }
      // The switch's label (LocalSessions.tsx `SHARE_LIVE_LABEL`), repeated: this file loads on every page.
      toast("Share every new session", {
        description: <span>Typing <code>claude</code> or <code>codex</code> in a terminal then starts it through Lightbulb, so you and your teammates see its live terminal. It adds a marked block to ~/.zshrc, ~/.bashrc and your bash login file. You can change it in Settings.</span>,
        duration: Infinity,
        action: {
          label: "Turn on",
          onClick: () => {
            invoke<boolean>("shell_integration_set", { enabled: true }).then(
              () => toast("Every new session is live. Open a new terminal tab to start one."),
              () => toast("Could not turn it on. Try it in Settings."),
            );
          },
        },
        cancel: { label: "Not now", onClick: () => {} },
      });
    }, () => {});
    return () => { live = false; };
  }, []);
  return null;
}
