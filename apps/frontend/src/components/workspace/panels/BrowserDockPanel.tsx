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
// The container's Chrome launches lazily on the FIRST browser tool call, so
// before this panel there was no way to see it without asking an agent to
// browse — and a connected VNC session legitimately showed a black desktop
// (the display with nothing drawn on it). Opening the panel now proactively
// starts Chrome via the browser.request/POST /start gateway RPC (the same
// ensure-running path the agent tool uses, minus the LLM turn), so the owner
// can watch/drive the browser without an agent — and without depending on
// inference being up. The start is idempotent (a no-op if Chrome is already
// running) and fire-and-forget: the VNC connect below runs independently and
// paints once Chrome draws, so a slow cold launch never blocks the panel.

import type { IDockviewPanelProps } from "dockview-react";
import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useRef, useState } from "react";
import { Chrome, RotateCcw } from "lucide-react";

import { BACKEND_URL } from "@/lib/api";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { browserViewWsUrl } from "./browserViewUrl";

type ViewState = "connecting" | "connected" | "disconnected";

export function BrowserDockPanel(_props: IDockviewPanelProps): React.ReactElement {
  const { getToken } = useAuth();
  const callRpc = useGatewayRpcMutation();
  const mountRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<ViewState>("connecting");
  // Bumping this re-runs the connect effect — the Retry button, and nothing
  // else; there is deliberately no auto-reconnect loop. A dead connection
  // usually means the container is asleep (trial scale-to-zero) or mid-deploy,
  // and a background retry loop would wake-spam the relay for a panel the
  // owner may not even be looking at.
  const [attempt, setAttempt] = useState(0);

  // Ensure the container's Chrome is running whenever the panel opens (and on
  // each Retry). Fire-and-forget: browser.request/POST /start ensures-then-
  // returns, is idempotent on an already-running browser, and can take up to
  // the backend RPC timeout on a cold 1-vCPU launch — none of which should
  // block the VNC connect below. A rejection (container asleep, RPC timeout
  // while the launch continues container-side) is swallowed; the VNC path owns
  // the visible state.
  useEffect(() => {
    void callRpc("browser.request", { method: "POST", path: "/start" }).catch(() => {});
  }, [attempt, callRpc]);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    let rfb: import("@novnc/novnc").default | null = null;
    let cancelled = false;
    setState("connecting");

    // Dynamic import: @novnc/novnc is ESM that touches browser globals at
    // module scope — importing it during SSR/prerender would throw. The token
    // rides the Sec-WebSocket-Protocol offer next to "binary" (the relay's
    // cross-host auth channel — the bv host never sees the Clerk cookie, and
    // a JS WebSocket cannot carry an Authorization header). The relay accepts
    // "binary"; the token never becomes the selected protocol.
    void Promise.all([import("@novnc/novnc"), getToken()]).then(([{ default: RFB }, token]) => {
      if (cancelled || !mountRef.current) return;
      if (!token) {
        setState("disconnected");
        return;
      }
      rfb = new RFB(mountRef.current, browserViewWsUrl(BACKEND_URL), {
        wsProtocols: ["binary", token],
      });
      // resizeSession: the panel drives the ACTUAL container display size —
      // noVNC sends SetDesktopSize, Xvnc resizes, matchbox re-fits Chrome —
      // so the browser renders edge-to-edge at native resolution instead of
      // being scaled like a video. scaleViewport stays on as the fallback for
      // the moments before the first resize lands (and for any server that
      // rejects the request), where it scales rather than cropping.
      rfb.resizeSession = true;
      rfb.scaleViewport = true;
      rfb.background = "transparent";
      rfb.addEventListener("connect", () => {
        if (!cancelled) setState("connected");
      });
      rfb.addEventListener("disconnect", () => {
        if (!cancelled) setState("disconnected");
      });
    }).catch(() => {
      // getToken() rejecting (expired session, network blip) or the dynamic
      // import failing must land on the disconnected state — leaving the
      // spinner up hides the Retry button behind a failure it can't see.
      if (!cancelled) setState("disconnected");
    });

    return () => {
      cancelled = true;
      try {
        rfb?.disconnect();
      } catch {
        // Already closed — noVNC throws on double-disconnect.
      }
    };
    // getToken is identity-stable from Clerk; listed for lint-completeness,
    // not as a real retrigger.
  }, [attempt, getToken]);

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
                Browser view isn&rsquo;t reachable. Your agents may be asleep, or the container is
                still starting — Retry once it&rsquo;s awake.
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
