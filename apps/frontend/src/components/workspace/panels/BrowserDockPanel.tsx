"use client";

// Live view of the agent's container browser — a real VNC session, not a
// screenshot feed. @novnc/novnc paints the container's Xvfb framebuffer to a
// canvas and sends mouse/keyboard back over the same WebSocket, so the owner
// can watch the agent browse AND take over a step it can't finish alone (an
// OAuth consent screen, a CAPTCHA, a 2FA prompt) without the agent losing its
// place.
//
// Transport: wss to the backend's owner-scoped relay
// (routers/browser_view.py) → the container's websockify → x11vnc (spawned
// per-connection). Auth rides the Clerk session cookie on the upgrade — same
// mechanism as the paperclip live-events relay, and the reason there is no
// token in this file.
//
// The container's Chromium launches lazily on the agent's FIRST browser tool
// call, so a connected session can legitimately show an empty gray desktop —
// that is the display with nothing drawn on it yet, not a bug. The hint line
// in the connected state exists for exactly that moment.

import type { IDockviewPanelProps } from "dockview-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Chrome, RotateCcw } from "lucide-react";

import { BACKEND_URL } from "@/lib/api";

type ViewState = "connecting" | "connected" | "disconnected";

const WS_URL = `${BACKEND_URL.replace(/^http/, "ws")}/browser-view/ws`;

export function BrowserDockPanel(_props: IDockviewPanelProps): React.ReactElement {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<ViewState>("connecting");
  // Bumping this re-runs the connect effect — the Retry button, and nothing
  // else; there is deliberately no auto-reconnect loop. A dead connection
  // usually means the container is asleep (trial scale-to-zero) or mid-deploy,
  // and a background retry loop would wake-spam the relay for a panel the
  // owner may not even be looking at.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    let rfb: import("@novnc/novnc").default | null = null;
    let cancelled = false;
    setState("connecting");

    // Dynamic import: @novnc/novnc is ESM that touches browser globals at
    // module scope — importing it during SSR/prerender would throw.
    void import("@novnc/novnc").then(({ default: RFB }) => {
      if (cancelled || !mountRef.current) return;
      rfb = new RFB(mountRef.current, WS_URL);
      // Scale the container's fixed 1280x800 display to whatever size this
      // dock panel currently has, rather than scrollbars/cropping.
      rfb.scaleViewport = true;
      rfb.background = "transparent";
      rfb.addEventListener("connect", () => {
        if (!cancelled) setState("connected");
      });
      rfb.addEventListener("disconnect", () => {
        if (!cancelled) setState("disconnected");
      });
    });

    return () => {
      cancelled = true;
      try {
        rfb?.disconnect();
      } catch {
        // Already closed — noVNC throws on double-disconnect.
      }
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return (
    <div className="relative flex h-full w-full flex-col bg-[var(--panel)]">
      {/* noVNC replaces the contents of this div with its canvas. */}
      <div ref={mountRef} className="min-h-0 flex-1" />

      {state !== "connected" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-center">
          <Chrome size={28} className="text-[var(--mute)]" />
          {state === "connecting" ? (
            <p className="text-sm text-[var(--mute)]">Connecting to your agents&rsquo; browser…</p>
          ) : (
            <>
              <p className="max-w-xs text-sm text-[var(--mute)]">
                Browser view isn&rsquo;t reachable. Your agents may be asleep, or their browser
                hasn&rsquo;t started yet — it launches the first time an agent uses it.
              </p>
              <button
                onClick={retry}
                className="flex items-center gap-1.5 rounded-md border border-[var(--line)] px-3 py-1.5 text-sm text-[var(--ink)] transition-colors hover:bg-[var(--hover)]"
              >
                <RotateCcw size={14} />
                Retry
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
