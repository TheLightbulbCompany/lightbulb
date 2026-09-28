"use client";

// ONE AWS DCV client per page. AgentCore streams a browser session to exactly one viewer
// (a second is refused at authentication), so the Browser panel and every "is browsing"
// card preview share one connection: the panel shows the real display, with input; each
// preview shows a `captureStream()` copy of its canvas. The connection is up while the
// session is live and something on the page watches it; with no panel on screen, the
// display is parked off screen and keeps drawing for the previews.

import { Suspense, lazy, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { useApi } from "@/lib/api";
import { ensureDcvMediaShim } from "./dcvWebKitShim";
import type { ViewerStatus } from "./DirectDcvLiveView";

// `lazy()` keeps the ~2 MB DCV SDK out of the shell chunk until something watches.
const LiveView = lazy(() => import("./DirectDcvLiveView").then((m) => ({ default: m.DirectDcvLiveView })));

export interface LiveSource {
  url: string;
  sessionId?: string | null;
  remoteWidth?: number;
  remoteHeight?: number;
}

/** The Browser panel on screen: the display moves into `slot` and takes input there. */
export interface PanelHold {
  slot: HTMLElement;
  menuHost: HTMLElement | null;
  busy: boolean;
}

interface State {
  source: LiveSource | null;
  watchers: number;
  panel: PanelHold | null;
  status: ViewerStatus;
}

let state: State = { source: null, watchers: 0, panel: null, status: "connecting" };
const listeners = new Set<() => void>();
const set = (patch: Partial<State>) => {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
};
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => void listeners.delete(listener);
};

export function useSharedLiveView(): State {
  return useSyncExternalStore(subscribe, () => state, () => state);
}

/** Watch the live browser while `source` is set (null: not watching). */
export function useLiveViewWatch(source: LiveSource | null): void {
  const on = !!source;
  useEffect(() => {
    if (!on) return;
    set({ watchers: state.watchers + 1 });
    return () => set({ watchers: state.watchers - 1 });
  }, [on]);
  const { url, sessionId, remoteWidth, remoteHeight } = source ?? {};
  useEffect(() => {
    if (url) set({ source: { url, sessionId, remoteWidth, remoteHeight } });
  }, [url, sessionId, remoteWidth, remoteHeight]);
}

/** The Browser panel holds the display while it is on screen. */
export function useLiveViewHold(hold: PanelHold | null): void {
  const { slot, menuHost, busy } = hold ?? {};
  useEffect(() => {
    if (!slot) return;
    set({ panel: { slot, menuHost: menuHost ?? null, busy: !!busy } });
    return () => set({ panel: null });
  }, [slot, menuHost, busy]);
}

let host: HTMLDivElement | null = null;
let parking: HTMLDivElement | null = null;

/** The biggest canvas DCV drew into (it draws into one), for a preview to mirror. */
export function liveViewCanvas(): HTMLCanvasElement | null {
  const canvases = [...(host?.querySelectorAll("canvas") ?? [])];
  return canvases.sort((a, b) => b.width * b.height - a.width * a.height)[0] ?? null;
}

/** Mounted once per page: owns the one DCV client. */
export function SharedLiveViewHost(): React.ReactElement | null {
  const api = useApi();
  const { source, watchers, panel } = useSharedLiveView();
  // Moving between panel and preview swaps watchers in one commit, so the count never
  // touches zero and nothing reconnects. With nothing watching, the slot is let go at once
  // for whoever watches next (another person, another device).
  const on = !!source && watchers > 0;

  const [element] = useState(() => {
    if (typeof document === "undefined") return null;
    host ??= Object.assign(document.createElement("div"), { className: "absolute inset-0" });
    return host;
  });
  const slot = panel?.slot;
  useEffect(() => {
    if (!element) return;
    if (!slot && !parking) {
      // Off screen, not hidden: DCV keeps drawing, so previews keep moving.
      parking = document.createElement("div");
      parking.setAttribute("aria-hidden", "true");
      parking.style.cssText = "position:fixed;left:-20000px;top:0;width:1280px;height:720px;overflow:hidden;pointer-events:none";
      document.body.appendChild(parking);
    }
    (slot ?? parking)?.appendChild(element);
  }, [element, slot]);

  const sessionId = source?.sessionId;
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;
  if (!element || !on || !source || !ensureDcvMediaShim()) return null;

  // A reconnect's fresh URL; a different session comes back through the watchers instead.
  const resign = async () => {
    try {
      const body = await api.get("/browser/live-view") as { active?: boolean; url?: string | null; sessionId?: string | null };
      return body.active && body.url && body.sessionId === sessionRef.current ? body.url : null;
    } catch {
      return null;
    }
  };
  // Sizes the remote window to the panel; the backend declines while an agent drives it.
  const resizeRemote = async (size: { width: number; height: number }) => {
    try {
      return ((await api.post("/browser/viewport", size)) as { resized?: boolean }).resized === true;
    } catch {
      return false;
    }
  };
  return createPortal(
    <Suspense fallback={null}>
      <LiveView key={source.sessionId ?? source.url} sessionId={source.sessionId} signedUrl={source.url} resign={resign}
        remoteWidth={source.remoteWidth} remoteHeight={source.remoteHeight} onStatus={(status) => set({ status })}
        menuHost={panel?.menuHost} busy={panel?.busy ?? true} resizeRemote={panel ? resizeRemote : undefined} />
    </Suspense>,
    element,
  );
}

/** A preview: a muted copy of the one display, no input. */
export function LiveViewMirror({ className }: { className?: string }): React.ReactElement {
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    let mirrored: HTMLCanvasElement | null = null;
    // ponytail: polls for the canvas (DCV makes it on connect, a reconnect may replace it); a
    // MutationObserver on the host if 500 ms ever shows.
    const attach = () => {
      const canvas = liveViewCanvas();
      if (!canvas || canvas === mirrored || !video.current || !canvas.captureStream) return;
      mirrored = canvas;
      video.current.srcObject = canvas.captureStream();
    };
    attach();
    const timer = setInterval(attach, 500);
    const element = video.current;
    return () => {
      clearInterval(timer);
      if (element) element.srcObject = null;
    };
  }, []);
  return <video ref={video} autoPlay muted playsInline className={className} data-testid="browser-mirror" />;
}

export function _resetSharedLiveViewForTests(): void {
  state = { source: null, watchers: 0, panel: null, status: "connecting" };
  host?.remove();
  parking?.remove();
  host = null;
  parking = null;
}
