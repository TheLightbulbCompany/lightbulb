"use client";

// The one right pane: one thing at a time (a thread, a file, the browser, an
// agent's profile, a task's detail, mail — Ideas joins in Task 10b as one more
// PanelKind). Resizable from its left edge, closable, and its width is
// remembered per person; the browser opens at half the width. Expand gives it
// the screen beside a narrow chat column (WorkspaceShell). Below 600 px it is a
// full-screen sheet over the main view (workspace.css).
import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { Maximize2, Minimize2, X } from "lucide-react";
import type { PanelKind } from "./panelRegistry";
import { PANEL_COMPONENTS } from "./panelComponents";
import { closePane, setPaneFullScreen, setThreadPaneHost, type Slot } from "./workspaceController";

export const PANE_MIN_WIDTH = 320;
export const PANE_DEFAULT_WIDTH = 440;
/** The main view (or the full-screen chat column) keeps at least this much. */
export const MAIN_MIN_WIDTH = 360;
const KEY_STEP = 16;

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
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      commit(px);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  };
  const nudge = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowLeft" ? KEY_STEP : event.key === "ArrowRight" ? -KEY_STEP : 0;
    const paneEl = event.currentTarget.parentElement;
    if (!step || !paneEl) return;
    event.preventDefault();
    commit(clampTo(paneEl, paneEl.getBoundingClientRect().width + step));
  };

  const Panel = pane.component === "thread" ? null : PANEL_COMPONENTS[pane.component as PanelKind];
  return (
    <section className="workspace-pane" aria-label={pane.title || "Side pane"} data-pane={pane.component}
      style={fullScreen ? undefined : { width }}>
      {!fullScreen && <div className="workspace-pane__resize" role="separator" aria-orientation="vertical"
        aria-label="Resize the pane" tabIndex={0} onPointerDown={startResize} onKeyDown={nudge} />}
      <header className="workspace-pane__bar">
        <span className="workspace-pane__title">{pane.title}</span>
        <button type="button" className="workspace-pane__full" aria-label={fullScreen ? "Exit full screen" : "Full screen"}
          title={fullScreen ? "Exit full screen" : "Full screen"} onClick={() => setPaneFullScreen(!fullScreen)}>
          {fullScreen ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />}
        </button>
        <button type="button" aria-label="Close pane" title="Close" onClick={closePane}><X size={16} aria-hidden="true" /></button>
      </header>
      <div className="workspace-pane__body">
        {Panel ? <Panel id={pane.panelId} params={pane.params} />
          // The room portals its side column (thread, tasks, details) here (useThreadPane).
          : <div ref={setThreadPaneHost} className="room-context-host workspace-pane__thread" />}
      </div>
    </section>
  );
}
