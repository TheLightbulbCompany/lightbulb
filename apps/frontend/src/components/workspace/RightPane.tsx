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
import { PaneActionsHost, PaneHeader } from "./PaneHeader";
import { leavePane } from "./panels/AutomationDockPanel";
import { readableName } from "@/components/creations/docText";

/** The narrowest a list pane is dragged to (super-agent; 320 without it). */
export const PANE_MIN_WIDTH = 360;
const LEGACY_PANE_MIN_WIDTH = 320;
/** The list width (Tasks, Files, Scheduled): stored per person when dragged. */
export const PANE_DEFAULT_WIDTH = 440;
/** Super-agent: one width for a list and an item (a task, a doc), about half the main area, at least 520, never more
 *  than 60%, until the person drags it; then their width for both. The header's toggles stay put going ‹ Tasks
 *  (W16-10: a 590 task and a 500 list moved them ~90 px under the pointer). */
export const PANE_ITEM_MIN_WIDTH = 520;
const ITEM_WIDTH = `clamp(${PANE_ITEM_MIN_WIDTH}px, 50%, 60%)`;
/** The main view (or the full-screen chat column) keeps at least this much. */
export const MAIN_MIN_WIDTH = 360;
const KEY_STEP = 16;
// Super-agent, ≥1000 px: the sidebar takes 56 + 264, so the main area is the window less 320. Below these widths the
// main column would fall under 360 beside a list (at least 360) or an item (520), and the pane opens as a sheet instead.
const CROWDED_LIST = 56 + 264 + MAIN_MIN_WIDTH + PANE_MIN_WIDTH - 1;
const CROWDED_ITEM = 56 + 264 + MAIN_MIN_WIDTH + PANE_ITEM_MIN_WIDTH - 1;
const OWN_HEADER: ReadonlySet<Slot["component"]> = new Set(["thread", "task", "automation"]);
// The specialist's own name heads its pane (AgentInspectorPanel), so the bar carries no second title.
const ownsHeader = (pane: Slot) => OWN_HEADER.has(pane.component) || pane.panelId === "agent-inspector";

export const paneWidthKey = (userId: string | null | undefined) => `lightbulb.pane.width.${userId ?? "signed-out"}`;

/** A single item, not a list: it opens at the item width (super-agent). A doc is one too, so opening a task's doc
 *  keeps the task's width (W13-7). */
export const isPaneItem = (pane: Pick<Slot, "component" | "params">): boolean =>
  pane.component === "task" || pane.component === "automation" || pane.component === "artifact"
  || (pane.component === "thread" && pane.params.item === "1");

/** A doc reads by its own first heading (ArtifactDockPanel sets `heading` for the doc `headingOf`), else a readable name, never its file name. */
const paneTitle = (pane: Slot): string =>
  pane.component !== "artifact" ? pane.title
    : (pane.params.headingOf === pane.params.id && pane.params.heading) || (pane.params.name ? readableName(pane.params.name) : pane.title);

export function readPaneWidth(key: string, min = LEGACY_PANE_MIN_WIDTH): number {
  return storedPaneWidth(key, min) ?? PANE_DEFAULT_WIDTH;
}

/** The width the person dragged to, if any. */
function storedPaneWidth(key: string, min: number): number | null {
  try {
    const stored = Number(window.localStorage.getItem(key));
    return stored >= min ? stored : null;
  } catch {
    return null;
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
  const superAgent = useSuperAgent();
  const minWidth = superAgent ? PANE_MIN_WIDTH : LEGACY_PANE_MIN_WIDTH;
  const key = paneWidthKey(userId);
  // The browser always opens at about half the width; its drags are not remembered.
  const remembers = pane.component !== "browser";
  const [width, setWidth] = useState<string>(() => {
    if (!remembers) return "50%";
    if (!superAgent) return `${readPaneWidth(key, minWidth)}px`;
    const stored = storedPaneWidth(key, minWidth);
    return stored ? `${stored}px` : ITEM_WIDTH;
  });
  // Super-agent: an item is one thing (it may open as a sheet sooner); list and item share the pane's one width.
  const item = superAgent && isPaneItem(pane);
  // The separator's value in px: measured, since the Browser's opening width is a percentage.
  const paneRef = useRef<HTMLElement>(null);
  const [measured, setMeasured] = useState({ now: 0, max: 0 });
  useLayoutEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;
    const now = Math.round(pane.getBoundingClientRect().width);
    const max = Math.round((pane.parentElement?.getBoundingClientRect().width ?? 0) - MAIN_MIN_WIDTH);
    setMeasured((prev) => prev.now === now && prev.max === max ? prev : { now, max });
  }, [width, fullScreen, item]);

  const clampTo = (pane: HTMLElement, px: number) => {
    const room = (pane.parentElement?.getBoundingClientRect().width ?? Infinity) - MAIN_MIN_WIDTH;
    return Math.max(minWidth, Math.min(px, room));
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
  // Super-agent: also wherever the main column would drop under 360 beside the pane (an item needs 520).
  const narrow = useMediaQuery(paneSheetQuery(superAgent));
  // At the shared item width a list crowds where an item does: going ‹ Tasks never swaps a docked pane for a sheet.
  const crowded = useMediaQuery(`(max-width: ${superAgent ? (item || width === ITEM_WIDTH ? CROWDED_ITEM : CROWDED_LIST) : 0}px)`) && superAgent;
  const sheet = (narrow || crowded) && !fullScreen;
  // Who opened it, read while mounting (before the panel's own effects move focus): focus goes back there on close.
  const [opener] = useState(() => typeof document === "undefined" ? null : document.activeElement as HTMLElement | null);
  const closeRef = useRef<HTMLButtonElement>(null);
  // The drawn header's action slot: the panel portals its own (a doc's Copy, Download) beside Expand and ✕.
  const [actionsHost, setActionsHost] = useState<HTMLElement | null>(null);
  useEffect(() => {
    const el = paneRef.current;
    if (!sheet || !el?.parentElement) return;
    const parent = el.parentElement;
    // Focus starts in the sheet (its close button) unless the panel already put it there.
    if (!el.contains(document.activeElement)) (closeRef.current ?? el.querySelector<HTMLElement>("[data-pane-close]"))?.focus();
    // What it covers is inert, including a view that mounts while it is open.
    const covered = new Set<Element>();
    const cover = () => {
      for (const c of parent.children) if (c !== el && !c.hasAttribute("inert") && !c.classList.contains("workspace-pane-scrim")) { c.setAttribute("inert", ""); covered.add(c); }
    };
    cover();
    const observer = new MutationObserver(cover);
    observer.observe(parent, { childList: true });
    return () => { observer.disconnect(); covered.forEach((c) => c.removeAttribute("inert")); };
  }, [sheet]);
  useEffect(() => () => {
    // Closed: back to whatever opened it (a sheet, or a docked pane closed by Escape from its tile, W17-1), if it is
    // still there and focus has nowhere better.
    const active = document.activeElement;
    if (opener?.isConnected && (!active || active === document.body || !active.isConnected)) opener.focus();
  }, [opener]);

  const Panel = pane.component === "thread" ? null : PANEL_COMPONENTS[pane.component as PanelKind];
  // Super-agent: no shell bar. An item that carries its own header (the room's column, a task, an automation) draws
  // the one PaneHeader; every other kind gets one here, titled by what opened it.
  const resizable = !fullScreen && !(superAgent && sheet);
  // Super-agent: RightPane draws the header for every kind without its own (the specialist profile included: its
  // name heads the body, so the bar used to carry no ✕ at all), and Escape closes those at every width.
  const drawsHeader = superAgent && !OWN_HEADER.has(pane.component);
  const escapes = sheet || drawsHeader;
  // A file opened from inside the pane (a task's output, Made here, Files) goes back there: "‹ Task", and Escape.
  const back = pane.params.back || null;
  const crumb = back ? { label: back.startsWith("task:") ? "Task" : back[0].toUpperCase() + back.slice(1), onBack: () => leavePane(back) } : undefined;
  // One Escape stack (doc → task → list → closed) at every width: Escape with focus outside the pane (on the page body,
  // or still on the tile / chip that opened it, W17-1) is Escape in the pane's innermost column, so it pops one level
  // there instead of doing nothing. Text fields, menus and dialogs keep their own Escape.
  useEffect(() => {
    if (!superAgent) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (event.key !== "Escape" || event.defaultPrevented || !paneRef.current || (target && paneRef.current.contains(target))) return;
      if (target?.closest("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='menu'], [role='listbox'], [data-radix-popper-content-wrapper]")) return;
      if (document.querySelector("[role='alertdialog'], [role='dialog']:not(.workspace-pane)")) return;
      const titles = paneRef.current?.querySelectorAll<HTMLElement>(".pane-header__title");
      const inner = titles?.[titles.length - 1] ?? paneRef.current;
      if (!inner) return;
      inner.focus({ preventScroll: true });
      inner.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [superAgent]);
  return (<>
    {superAgent && sheet && <div className="workspace-pane-scrim" aria-hidden="true" onClick={closePane} />}
    <section ref={paneRef} className="workspace-pane" aria-label={pane.title} data-pane={pane.component}
      data-item={item || undefined} data-sheet={(superAgent && sheet) || undefined}
      {...(sheet ? { role: "dialog", "aria-modal": true } : {})}
      // Inner columns handle their own Escape first (and stop it); what reaches here closes the sheet.
      onKeyDown={escapes ? (event) => {
        if (event.key === "Escape" && !event.defaultPrevented) { if (back) leavePane(back); else closePane(); }
        else if (sheet) trapTab(event, [paneRef.current]);
      } : undefined}
      style={fullScreen || (superAgent && sheet) ? undefined : { width }}>
      {resizable && <div className="workspace-pane__resize" role="separator" aria-orientation="vertical"
        aria-label="Resize the pane" tabIndex={0} onPointerDown={startResize} onKeyDown={nudge}
        aria-valuenow={measured.now} aria-valuemin={minWidth} aria-valuemax={Math.max(minWidth, measured.max)} />}
      {superAgent ? drawsHeader && <PaneHeader title={paneTitle(pane)} crumb={crumb} actions={<span ref={setActionsHost} className="contents" />} />
        : <header className="workspace-pane__bar">
          {/* A thread (the room's column, a task) carries its own titled header: the bar keeps only its controls. */}
          <span className="workspace-pane__title">{ownsHeader(pane) ? null : pane.title}</span>
          <button type="button" className="workspace-pane__full" aria-label={fullScreen ? "Exit full screen" : "Full screen"}
            title={fullScreen ? "Exit full screen" : "Full screen"} onClick={() => setPaneFullScreen(!fullScreen)}>
            {fullScreen ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />}
          </button>
          <button ref={closeRef} type="button" aria-label="Close pane" title="Close panel" onClick={closePane}><X size={16} aria-hidden="true" /></button>
        </header>}
      <div className="workspace-pane__body">
        {Panel ? <PaneActionsHost.Provider value={actionsHost}><Panel id={pane.panelId} params={pane.params} /></PaneActionsHost.Provider>
          // The room portals its side column (thread, tasks, details) here (useThreadPane).
          : <div ref={setThreadPaneHost} className="room-context-host workspace-pane__thread" />}
      </div>
    </section>
  </>);
}
