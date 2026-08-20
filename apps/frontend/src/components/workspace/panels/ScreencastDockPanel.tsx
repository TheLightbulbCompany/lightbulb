"use client";

// Live view of the agent's container browser via a CDP screencast — the modern
// replacement for the VNC panel (BrowserDockPanel.tsx, still present as the
// fallback path). Instead of noVNC painting the whole Xvfb framebuffer, the
// backend relay streams ONE CDP page target as JPEG frames
// (Page.startScreencast); we paint them to a <canvas> and send human
// mouse/keyboard back as CDP Input.* events. Resolution follows the page
// viewport, not a fixed 1280x800 X display — which is the whole point (fixes the
// blur and the matchbox fullscreen trap). The viewport in turn follows the
// PANEL: a ResizeObserver reports the panel's CSS-pixel box over the relay
// ({type:"resize"}), which applies Emulation.setDeviceMetricsOverride so the
// page reflows to the panel like a real resizing window instead of stretching.
//
// Transport: wss to the backend's owner-scoped screencast relay
// (routers/screencast.py) over the SAME bv door + auth as the VNC relay — the
// Clerk JWT rides the Sec-WebSocket-Protocol offer (the bv host never sees the
// Clerk cookie, and a JS WebSocket cannot carry an Authorization header).
//
// Panel contract (#1874): registered under the parameterless-singleton `browser`
// key, and — like the VNC panel — mounting ensures the container's Chrome is
// running via the browser.request/POST /start gateway RPC (idempotent,
// fire-and-forget). The screencast connect runs independently and paints once
// Chrome draws.
//
// Rollout gate is the image pin, not a code flag: on a container whose image
// lacks the CDP proxy the relay closes and this panel shows its disconnected
// state (Retry). dev.tag -> validate -> prod.tag is the staging gate.

import type { IDockviewPanelProps } from "dockview-react";
import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Chrome, Globe, Plus, RotateCcw, RotateCw, X } from "lucide-react";

import { BACKEND_URL } from "@/lib/api";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { screencastWsUrl } from "./browserViewUrl";
import { normalizeTabs, tabLabel, type BrowserTab } from "./browserTabs";
import { normalizeBrowserUrlDraft } from "./browserNav";
import {
  mapPointerToDevice,
  toCdpKeyEvent,
  toCdpMouseEvent,
  type ScreencastMetadata,
} from "./screencastInput";

type ViewState = "connecting" | "connected" | "disconnected";

export function ScreencastDockPanel(_props: IDockviewPanelProps): React.ReactElement {
  const { getToken } = useAuth();
  const callRpc = useGatewayRpcMutation();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // Last frame's metadata (deviceWidth/Height) — the mapping input for turning a
  // canvas pointer into CDP CSS-px coords. Held in a ref so input handlers read
  // the latest without re-subscribing.
  const metaRef = useRef<ScreencastMetadata>({ deviceWidth: 0, deviceHeight: 0 });
  const [state, setState] = useState<ViewState>("connecting");
  // Retry button only; no auto-reconnect loop (an asleep/mid-deploy container
  // would get wake-spammed), same policy as the VNC panel.
  const [attempt, setAttempt] = useState(0);
  const [tabs, setTabs] = useState<BrowserTab[]>([]);
  // The RAW CDP target id the relay is streaming (echoed by its {type:"target"}
  // message). The chrome addresses tabs two ways: the RAW id highlights the
  // active tab + is what ?target= reconnects to (the relay/CDP only knows raw
  // ids), while browser.request calls (navigate/focus/act/close) use the tab's
  // stable ALIAS id, which the plugin resolves server-side.
  const [activeTargetId, setActiveTargetId] = useState<string | null>(null);
  // URL bar: while editing, show the user's draft; otherwise mirror the active
  // tab's live URL (derived at render — no setState-in-effect sync needed).
  const [editing, setEditing] = useState(false);
  const [urlDraft, setUrlDraft] = useState("");
  // Raw target the NEXT (re)connect should stream; null = let the relay pick the
  // first page target. Read at connect time; a switch sets it then bumps attempt.
  const desiredTargetRef = useRef<string | null>(null);
  const refreshSeq = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Ensure the container's Chrome is running whenever the panel opens (and on
  // each Retry). Fire-and-forget + idempotent — the panel-contract requirement.
  useEffect(() => {
    void callRpc("browser.request", { method: "POST", path: "/start" }).catch(() => {});
  }, [attempt, callRpc]);

  // Send a JSON input message to the relay (dropped silently if not open).
  const send = useCallback((msg: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }, []);

  // Ask the relay to match the container browser's viewport to the panel box so
  // the page REFLOWS to the panel size (the relay applies
  // Emulation.setDeviceMetricsOverride, aspect-preserving-capped server-side).
  // deviceScaleFactor is deliberately 1, NOT window.devicePixelRatio: frame
  // production in the container is CPU-bound under SwiftShader software
  // rendering and scales with pixel count — dsf 2 would quadruple frame cost
  // (~5fps -> ~2fps) for crispness a JPEG stream doesn't repay. The relay
  // forces 1 anyway; sending the field keeps the wire shape ready for GPU boxes.
  const sendResize = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.round(rect.width);
    const height = Math.round(rect.height);
    if (width <= 0 || height <= 0) return; // pre-layout / hidden panel
    send({ type: "resize", width, height, deviceScaleFactor: 1 });
  }, [send]);

  // Panel-size tracking, trailing-debounced ~150ms so a splitter drag coalesces
  // into one viewport override (the relay coalesces again as the server fence).
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof ResizeObserver === "undefined") return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const observer = new ResizeObserver(() => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(sendResize, 150);
    });
    observer.observe(canvas);
    return () => {
      if (timer) clearTimeout(timer);
      observer.disconnect();
    };
  }, [sendResize]);

  useEffect(() => {
    // State is driven by the WS lifecycle callbacks below (onopen → connected,
    // onclose/onerror → disconnected) and reset to "connecting" by the initial
    // useState + the retry handler — never synchronously in this effect body.
    let cancelled = false;
    let ws: WebSocket | null = null;

    void getToken()
      .then((token) => {
        if (cancelled) return;
        if (!token) {
          setState("disconnected");
          return;
        }
        // Token rides the subprotocol offer next to the accept token, same
        // cross-host auth channel as the VNC relay. The relay selects
        // "isol8-screencast"; the JWT never becomes the selected protocol.
        ws = new WebSocket(screencastWsUrl(BACKEND_URL, desiredTargetRef.current), [
          "isol8-screencast",
          token,
        ]);
        wsRef.current = ws;

        ws.onopen = () => {
          if (cancelled) return;
          setState("connected");
          // Match the viewport to the panel right away; frames arriving before
          // the override land at the browser's previous size and render as a
          // brief stretch — visually a window mid-resize.
          sendResize();
        };
        ws.onclose = () => {
          if (!cancelled) setState("disconnected");
        };
        ws.onerror = () => {
          if (!cancelled) setState("disconnected");
        };
        ws.onmessage = (ev) => {
          if (cancelled) return;
          let frame: { type?: string; data?: string; metadata?: ScreencastMetadata; targetId?: string };
          try {
            frame = JSON.parse(typeof ev.data === "string" ? ev.data : "");
          } catch {
            return;
          }
          // The relay's opening frame names the raw target it's streaming, so the
          // tab strip highlights the right tab + the URL bar shows its address.
          if (frame.type === "target") {
            setActiveTargetId(typeof frame.targetId === "string" ? frame.targetId : null);
            return;
          }
          if (frame.type !== "frame" || !frame.data) return;
          const meta = frame.metadata;
          if (meta && meta.deviceWidth > 0 && meta.deviceHeight > 0) metaRef.current = meta;
          const canvas = canvasRef.current;
          if (!canvas) return;
          const img = new Image();
          img.onload = () => {
            if (cancelled) return;
            const ctx = canvas.getContext("2d");
            if (!ctx) return;
            // Match backing store to the frame so drawImage is 1:1; CSS (objectFit
            // "fill" below) stretches it to fill the panel box exactly. Because
            // "fill" makes the displayed rect equal the painted frame, the input
            // mapping (displayed rect + metadata) has no letterbox offset and is
            // independent of this backing size.
            if (canvas.width !== img.naturalWidth) canvas.width = img.naturalWidth;
            if (canvas.height !== img.naturalHeight) canvas.height = img.naturalHeight;
            ctx.drawImage(img, 0, 0);
          };
          // ponytail: new Image per frame (browser decodes JPEG); switch to
          // createImageBitmap if CPU shows up in the live measurement.
          img.src = `data:image/jpeg;base64,${frame.data}`;
        };
      })
      .catch(() => {
        if (!cancelled) setState("disconnected");
      });

    return () => {
      cancelled = true;
      try {
        ws?.close();
      } catch {
        // already closed
      }
      wsRef.current = null;
    };
  }, [attempt, getToken, sendResize]);

  // ---- Input capture -> CDP Input.* (via the relay) ----
  const canvasPos = useCallback((e: { clientX: number; clientY: number }) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    return mapPointerToDevice(e.clientX, e.clientY, canvas.getBoundingClientRect(), metaRef.current);
  }, []);

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => send({ type: "mouse", event: toCdpMouseEvent("mouseMoved", canvasPos(e), { buttons: e.buttons }) }),
    [send, canvasPos],
  );
  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      canvasRef.current?.focus();
      send({ type: "mouse", event: toCdpMouseEvent("mousePressed", canvasPos(e), { button: e.button, buttons: e.buttons, clickCount: e.detail || 1 }) });
    },
    [send, canvasPos],
  );
  const onMouseUp = useCallback(
    (e: React.MouseEvent) => send({ type: "mouse", event: toCdpMouseEvent("mouseReleased", canvasPos(e), { button: e.button, buttons: e.buttons, clickCount: e.detail || 1 }) }),
    [send, canvasPos],
  );
  const onWheel = useCallback(
    (e: React.WheelEvent) => send({ type: "mouse", event: toCdpMouseEvent("mouseWheel", canvasPos(e), { deltaX: e.deltaX, deltaY: e.deltaY }) }),
    [send, canvasPos],
  );
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Leave focus management to the browser so the canvas is escapable: Tab
      // moves focus out normally, Escape blurs the canvas for an explicit exit.
      // Neither forwards to CDP. Every other key is captured and forwarded.
      // ponytail: no modifiers bitmask yet — Ctrl/Cmd shortcuts don't reach the
      // page (CodeRabbit's second point); wire modifiers in toCdpKeyEvent in the
      // later phase, per the plan.
      if (e.key === "Tab") return;
      if (e.key === "Escape") {
        canvasRef.current?.blur();
        return;
      }
      e.preventDefault();
      send({ type: "key", event: toCdpKeyEvent("keyDown", { key: e.key, code: e.code, keyCode: e.keyCode }) });
    },
    [send],
  );
  const onKeyUp = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Tab" || e.key === "Escape") return;
      e.preventDefault();
      send({ type: "key", event: toCdpKeyEvent("keyUp", { key: e.key, code: e.code, keyCode: e.keyCode }) });
    },
    [send],
  );

  // ---- Tab list (drives the chrome tab strip + URL bar; polled every 5s) ----
  const refreshTabs = useCallback(async () => {
    const seq = ++refreshSeq.current;
    const apply = (next: BrowserTab[]) => {
      if (mounted.current && seq === refreshSeq.current) setTabs(next);
    };
    try {
      apply(normalizeTabs(await callRpc("browser.request", { method: "GET", path: "/tabs" })));
    } catch {
      apply([]);
    }
  }, [callRpc]);

  useEffect(() => {
    if (state !== "connected") {
      // Invalidate any in-flight refresh; the strip renders only when
      // connected, so stale tabs stay invisible without a synchronous clear
      // here (reconnect's refreshTabs overwrites them).
      refreshSeq.current++;
      return;
    }
    void refreshTabs();
    const timer = setInterval(() => void refreshTabs(), 5000);
    return () => clearInterval(timer);
  }, [state, refreshTabs]);

  // The tab the stream is showing: matched by the relay's raw target id, else
  // the first tab (fresh connect before {type:"target"} lands).
  const activeTab = useMemo(
    () => tabs.find((t) => t.targetId === activeTargetId) ?? tabs[0] ?? null,
    [tabs, activeTargetId],
  );

  // The address the URL bar shows: the user's draft while editing, else the
  // active tab's live URL (updates on the tab poll with no effect/sync).
  const urlValue = editing ? urlDraft : (activeTab?.url ?? "");

  // Point the screencast at a raw target (or the first, when null) by reconnecting
  // — the only way to move a per-page stream. Reuses the retry() reconnect path;
  // state stays "connected" so the canvas holds the last frame (no overlay flash)
  // until the new tab paints.
  const reconnectTo = useCallback((rawTargetId: string | null) => {
    desiredTargetRef.current = rawTargetId;
    if (rawTargetId) setActiveTargetId(rawTargetId); // optimistic; relay confirms
    setAttempt((n) => n + 1);
  }, []);

  const switchTab = useCallback(
    (tab: BrowserTab) => {
      if (tab.targetId === activeTargetId) return;
      // Foreground it (so a background tab actually composites) then stream it.
      void callRpc("browser.request", { method: "POST", path: "/tabs/focus", body: { targetId: tab.id } }).catch(
        () => {},
      );
      reconnectTo(tab.targetId);
    },
    [activeTargetId, callRpc, reconnectTo],
  );

  const newTab = useCallback(() => {
    void callRpc("browser.request", { method: "POST", path: "/tabs/action", body: { action: "new" } })
      .then((res) => {
        const raw = (res as { tab?: { targetId?: unknown } } | null)?.tab?.targetId;
        if (typeof raw === "string" && raw) reconnectTo(raw);
      })
      .catch(() => {})
      .finally(() => void refreshTabs());
  }, [callRpc, reconnectTo, refreshTabs]);

  const closeTab = useCallback(
    (tab: BrowserTab) => {
      void callRpc("browser.request", { method: "DELETE", path: `/tabs/${encodeURIComponent(tab.id)}` })
        .catch(() => {})
        .finally(() => {
          void refreshTabs();
          // Closed the streamed tab → fall the stream back to the first remaining.
          if (tab.targetId === activeTargetId) reconnectTo(null);
        });
    },
    [activeTargetId, callRpc, reconnectTo, refreshTabs],
  );

  const navigateTo = useCallback(
    (raw: string) => {
      const url = normalizeBrowserUrlDraft(raw);
      if (!url || !activeTab) return;
      void callRpc("browser.request", {
        method: "POST",
        path: "/navigate",
        body: { url, targetId: activeTab.id },
      })
        .catch(() => {})
        .finally(() => void refreshTabs());
    },
    [activeTab, callRpc, refreshTabs],
  );

  // Reload = re-navigate to the current URL (the fork's reloadPage: a live
  // document reload, not just a repaint). No dedicated reload route exists.
  const reload = useCallback(() => {
    if (activeTab?.url) navigateTo(activeTab.url);
  }, [activeTab, navigateTo]);

  // Back/forward via the browser tool's evaluate route (history.go) — the same
  // path the fork's own Control-UI browser panel uses; evaluate is enabled by
  // default in the container config. No back/forward HTTP route exists.
  const goHistory = useCallback(
    (delta: -1 | 1) => {
      if (!activeTab) return;
      void callRpc("browser.request", {
        method: "POST",
        path: "/act",
        body: { kind: "evaluate", targetId: activeTab.id, fn: `() => { history.go(${delta}); return true; }` },
      })
        .catch(() => {})
        .finally(() => window.setTimeout(() => void refreshTabs(), 700));
    },
    [activeTab, callRpc, refreshTabs],
  );

  const retry = useCallback(() => {
    setState("connecting");
    setAttempt((n) => n + 1);
  }, []);

  return (
    <div className="relative flex h-full w-full flex-col bg-[var(--panel)]">
      {/* Browser chrome: a REAL flex child above the canvas (not an overlay), so
          the canvas box shrinks by the chrome height and mapPointerToDevice —
          which reads the canvas's own getBoundingClientRect — stays exact, and
          the ResizeObserver reflows the container viewport to the smaller box. */}
      {state === "connected" && (
        <div className="flex shrink-0 flex-col border-b border-[var(--line)] bg-[var(--panel)]">
          {/* Tab strip */}
          <div className="flex items-center gap-1 overflow-x-auto px-1.5 pt-1.5">
            {tabs.map((tab) => {
              const active = tab.targetId === activeTargetId;
              const label = tabLabel(tab);
              return (
                <div
                  key={tab.id}
                  role="tab"
                  aria-selected={active}
                  title={tab.url || label}
                  onClick={() => switchTab(tab)}
                  className={`flex min-w-0 max-w-[200px] shrink-0 cursor-pointer items-center gap-1.5 rounded-t-md border border-b-0 px-2 py-1 text-xs ${
                    active
                      ? "border-[var(--line)] bg-[var(--hover)] text-[var(--ink)]"
                      : "border-transparent text-[var(--mute)] hover:bg-[var(--hover)] hover:text-[var(--ink)]"
                  }`}
                >
                  <Globe size={12} className="shrink-0 opacity-70" />
                  <span className="truncate">{label}</span>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      closeTab(tab);
                    }}
                    aria-label={`Close ${label}`}
                    className="ml-0.5 shrink-0 rounded p-0.5 text-[var(--mute)] transition-colors hover:bg-[var(--line)] hover:text-[var(--ink)]"
                  >
                    <X size={11} />
                  </button>
                </div>
              );
            })}
            <button
              onClick={newTab}
              aria-label="New tab"
              className="shrink-0 rounded p-1 text-[var(--mute)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--ink)]"
            >
              <Plus size={14} />
            </button>
          </div>

          {/* Toolbar: back / forward / reload + address bar */}
          <div className="flex items-center gap-1 px-1.5 py-1.5">
            <button
              onClick={() => goHistory(-1)}
              aria-label="Back"
              className="rounded p-1 text-[var(--mute)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--ink)]"
            >
              <ArrowLeft size={16} />
            </button>
            <button
              onClick={() => goHistory(1)}
              aria-label="Forward"
              className="rounded p-1 text-[var(--mute)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--ink)]"
            >
              <ArrowRight size={16} />
            </button>
            <button
              onClick={reload}
              aria-label="Reload"
              className="rounded p-1 text-[var(--mute)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--ink)]"
            >
              <RotateCw size={15} />
            </button>
            <form
              className="flex-1"
              onSubmit={(e) => {
                e.preventDefault();
                navigateTo(urlValue);
                canvasRef.current?.focus();
              }}
            >
              <input
                type="text"
                inputMode="url"
                spellCheck={false}
                autoComplete="off"
                aria-label="Address bar"
                placeholder="Enter a URL"
                value={urlValue}
                onChange={(e) => setUrlDraft(e.target.value)}
                onFocus={(e) => {
                  setEditing(true);
                  setUrlDraft(activeTab?.url ?? "");
                  e.currentTarget.select();
                }}
                onBlur={() => setEditing(false)}
                className="w-full truncate rounded-md border border-[var(--line)] bg-[var(--panel)] px-2.5 py-1 text-xs text-[var(--ink)] outline-none focus:border-[var(--forest)]"
              />
            </form>
          </div>
        </div>
      )}

      {/* The screencast canvas (fills the area below the chrome). tabIndex makes
          it focusable so it receives key events. objectFit "fill" (NOT "contain")
          stays deliberate now that the viewport tracks the panel: steady-state
          the frame's aspect EQUALS the canvas box's (the relay's cap is
          aspect-preserving), so fill has nothing to distort, and during the
          ~150ms resize debounce the transient mismatch renders as a brief
          stretch — visually a window mid-resize. "contain" would letterbox that
          transient AND reintroduce the letterbox offset in mapPointerToDevice;
          fill keeps the displayed rect equal to the painted frame, so
          proportional pointer mapping stays exact even mid-resize. */}
      <canvas
        ref={canvasRef}
        tabIndex={0}
        onMouseMove={onMouseMove}
        onMouseDown={onMouseDown}
        onMouseUp={onMouseUp}
        onWheel={onWheel}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        className="min-h-0 min-w-0 flex-1 outline-none"
        style={{ width: "100%", height: "100%", objectFit: "fill" }}
      />

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
