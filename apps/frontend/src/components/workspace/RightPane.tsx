"use client";

// The one right pane: one thing at a time (a thread, a file, the browser, an
// agent's profile, a task's detail, mail, Ideas). Resizable from its left edge, closable, and its width is
// remembered per person; the browser opens at half the width. Expand gives it
// the screen beside a narrow chat column (WorkspaceShell). Below 600 px it is a
// full-screen sheet over the main view (workspace.css).
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { Maximize2, Minimize2, X } from "lucide-react";
import type { PanelKind } from "./panelRegistry";
import { PANEL_COMPONENTS } from "./panelComponents";
import { closePane, setPaneFullScreen, setThreadPaneHost, type Slot } from "./workspaceController";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { trapTab } from "@/lib/focusTrap";
import { paneSheetQuery } from "./paneSheet";

export const PANE_MIN_WIDTH = 320;
export const PANE_DEFAULT_WIDTH = 440;
/** The main view (or the full-screen chat column) keeps at least this much. */
export const MAIN_MIN_WIDTH = 360;
const KEY_STEP = 16;
const OWN_HEADER: ReadonlySet<Slot["component"]> = new Set(["thread", "task"]);
// The specialist's own name heads its pane (AgentInspectorPanel), so the bar carries no second title.
const ownsHeader = (pane: Slot) => OWN_HEADER.has(pane.component) || pane.panelId === "agent-inspector";

export const paneWidthKey = (userId: string | null | undefined) => `lightbulb.pane.width.${userId ?? "signed-out"}`;

export function readPaneWidth(key: string): number {
  try {
    const stored = Number(window.localStorage.getItem(key));
    return stored >= PANE_MIN_WIDTH ? stored : PANE_DEFAULT_WIDTH;
  } catch {
    return PANE_DEFAULT_WIDTH;
  }
}

function writePaneWidth(key: string, width: number): void {
  try {
    window.localStorage.setItem(key, String(Math.round(width)));
  } catch {
    // Width memory is a per-viewer convenience; storage may refuse it.
  }
}

/** Keyed by the pane's panel id at the call site, so a new kind re-reads its opening width. */
export function RightPane({ pane, fullScreen }: { pane: Slot; fullScreen: boolean }): React.ReactElement {
  const { userId } = useAuth();
  const key = paneWidthKey(userId);
  // The browser always opens at about half the width; its drags are not remembered.
  const remembers = pane.component !== "browser";
  const [width, setWidth] = useState<string>(() => remembers ? `${readPaneWidth(key)}px` : "50%");
  // The separator's value in px: measured, since the Browser's opening width is a percentage.
  const paneRef = useRef<HTMLElement>(null);
  const [measured, setMeasured] = useState({ now: 0, max: 0 });
  useLayoutEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;
    const now = Math.round(pane.getBoundingClientRect().width);
    const max = Math.round((pane.parentElement?.getBoundingClientRect().width ?? 0) - MAIN_MIN_WIDTH);
    setMeasured((prev) => prev.now === now && prev.max === max ? prev : { now, max });
  }, [width, fullScreen]);

  const clampTo = (pane: HTMLElement, px: number) => {
    const room = (pane.parentElement?.getBoundingClientRect().width ?? Infinity) - MAIN_MIN_WIDTH;
    return Math.max(PANE_MIN_WIDTH, Math.min(px, room));
  };
  const commit = (px: number) => {
    setWidth(`${px}px`);
    if (remembers) writePaneWidth(key, px);
  };

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    const handle = event.currentTarget;
    const paneEl = handle.parentElement;
    if (!paneEl) return;
    const right = paneEl.getBoundingClientRect().right;
    handle.setPointerCapture?.(event.pointerId);
    let px = paneEl.getBoundingClientRect().width;
    const move = (next: PointerEvent) => {
      px = clampTo(paneEl, right - next.clientX);
      setWidth(`${px}px`);
    };
    // A cancelled drag (touch taken by the page, capture lost) still ends, and keeps its width.
    const ends = ["pointerup", "pointercancel", "lostpointercapture"] as const;
    const up = () => {
      handle.removeEventListener("pointermove", move);
      ends.forEach((kind) => handle.removeEventListener(kind, up));
      commit(clampTo(paneEl, px));
    };
    handle.addEventListener("pointermove", move);
    ends.forEach((kind) => handle.addEventListener(kind, up));
  };
  const nudge = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowLeft" ? KEY_STEP : event.key === "ArrowRight" ? -KEY_STEP : 0;
    const paneEl = event.currentTarget.parentElement;
    if (!step || !paneEl) return;
    event.preventDefault();
    commit(clampTo(paneEl, paneEl.getBoundingClientRect().width + step));
  };

  // A sheet over the main view (workspace.css): modal, so what it covers is inert, Tab stays in it and Escape closes it.
  const sheet = useMediaQuery(paneSheetQuery(useSuperAgent())) && !fullScreen;
  // Who opened it, read while mounting (before the panel's own effects move focus): focus goes back there on close.
  const [opener] = useState(() => typeof document === "undefined" ? null : document.activeElement as HTMLElement | null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const wasSheet = useRef(false);
  useEffect(() => {
    const el = paneRef.current;
    wasSheet.current = sheet;
    if (!sheet || !el?.parentElement) return;
    const parent = el.parentElement;
    // Focus starts in the sheet (its close button) unless the panel already put it there.
    if (!el.contains(document.activeElement)) closeRef.current?.focus();
    // What it covers is inert, including a view that mounts while it is open.
    const covered = new Set<Element>();
    const cover = () => {
      for (const c of parent.children) if (c !== el && !c.hasAttribute("inert")) { c.setAttribute("inert", ""); covered.add(c); }
    };
    cover();
    const observer = new MutationObserver(cover);
    observer.observe(parent, { childList: true });
    return () => { observer.disconnect(); covered.forEach((c) => c.removeAttribute("inert")); };
  }, [sheet]);
  useEffect(() => () => {
    // Closed while it was a sheet: back to whatever opened it, if it is still there and focus has nowhere better.
    const active = document.activeElement;
    if (wasSheet.current && opener?.isConnected && (!active || active === document.body || !active.isConnected)) opener.focus();
  }, [opener]);

  const Panel = pane.component === "thread" ? null : PANEL_COMPONENTS[pane.component as PanelKind];
  return (
    <section ref={paneRef} className="workspace-pane" aria-label={pane.title} data-pane={pane.component}
      {...(sheet ? { role: "dialog", "aria-modal": true } : {})}
      // Inner columns handle their own Escape first (and stop it); what reaches here closes the sheet.
      onKeyDown={sheet ? (event) => { if (event.key === "Escape") closePane(); else trapTab(event, [paneRef.current]); } : undefined}
      style={fullScreen ? undefined : { width }}>
      {!fullScreen && <div className="workspace-pane__resize" role="separator" aria-orientation="vertical"
        aria-label="Resize the pane" tabIndex={0} onPointerDown={startResize} onKeyDown={nudge}
        aria-valuenow={measured.now} aria-valuemin={PANE_MIN_WIDTH} aria-valuemax={Math.max(PANE_MIN_WIDTH, measured.max)} />}
      <header className="workspace-pane__bar">
        {/* A thread (the room's column, a task) carries its own titled header: the bar keeps only its controls. */}
        <span className="workspace-pane__title">{ownsHeader(pane) ? null : pane.title}</span>
        <button type="button" className="workspace-pane__full" aria-label={fullScreen ? "Exit full screen" : "Full screen"}
          title={fullScreen ? "Exit full screen" : "Full screen"} onClick={() => setPaneFullScreen(!fullScreen)}>
          {fullScreen ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />}
        </button>
        <button ref={closeRef} type="button" aria-label="Close pane" title="Close" onClick={closePane}><X size={16} aria-hidden="true" /></button>
      </header>
      <div className="workspace-pane__body">
        {Panel ? <Panel id={pane.panelId} params={pane.params} />
          // The room portals its side column (thread, tasks, details) here (useThreadPane).
          : <div ref={setThreadPaneHost} className="room-context-host workspace-pane__thread" />}
      </div>
    </section>
  );
}
