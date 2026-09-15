import { useEffect, useRef, useState, type CSSProperties, type RefObject } from "react";
import type { SidebarSort } from "./roomSections";

const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort; sessionColumnWidth?: number }
const EMPTY: SidebarPrefs = { collapsed: [], sort: "alpha" };

export const SESSION_COLUMN_MIN = 360;
// Floor 360px, ceiling 80% of the panel. Below ~450px wide the ceiling is
// LESS than the floor -- the middle pane must always keep at least 20%, so
// the ceiling wins even if that means a session column under 360px.
export const clampWidth = (px: number, panelWidth: number) => Math.min(Math.max(px, SESSION_COLUMN_MIN), Math.round(panelWidth * 0.8));

export function readSidebarPrefs(scope: string): SidebarPrefs {
  try {
    const raw = window.localStorage.getItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<SidebarPrefs>;
    return {
      collapsed: Array.isArray(parsed.collapsed) ? parsed.collapsed.filter((value) => typeof value === "string") : [],
      sort: parsed.sort === "unread" ? "unread" : "alpha",
      ...(typeof parsed.sessionColumnWidth === "number" && Number.isFinite(parsed.sessionColumnWidth) && parsed.sessionColumnWidth >= SESSION_COLUMN_MIN
        ? { sessionColumnWidth: parsed.sessionColumnWidth } : {}),
    };
  } catch {
    return EMPTY;
  }
}

export function writeSidebarPrefs(scope: string, prefs: SidebarPrefs): void {
  try {
    window.localStorage.setItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`, JSON.stringify(prefs));
  } catch {
    // Private windows and blocked site data: the sidebar simply forgets.
  }
}

/** Width of the session column for both placements, persisted per workspace. */
export function useSessionColumnWidth(scope: string, panel: RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState<number | undefined>(() => (typeof window === "undefined" ? undefined : readSidebarPrefs(scope).sessionColumnWidth));
  // `onCommit` must persist the width from the MOST RECENT nudge/drag, not the
  // `width` state closed over at the render that handed ColumnGrip its props --
  // a keyboard nudge calls onNudge() then onCommit() in the same handler,
  // before React has re-rendered with the new state. A ref is always current.
  const latest = useRef(width);
  // `width` only initializes from `scope` once (useState's initializer runs
  // on mount only) -- without this, switching workspaces without a remount
  // (the provider tree stays mounted, only `scope` changes) carries the
  // previous workspace's width over and can persist it under the new scope's
  // key on the next commit. Render-time "previous value" comparison, same
  // pattern as MessagingWorkspaceContext's per-room draft reset.
  const [seenScope, setSeenScope] = useState(scope);
  if (seenScope !== scope) {
    setSeenScope(scope);
    setWidth(typeof window === "undefined" ? undefined : readSidebarPrefs(scope).sessionColumnWidth);
  }
  // Mutating a ref during render is disallowed (react-hooks/refs), so the
  // scope-change branch above can only queue the state update -- this syncs
  // `latest` right after, before the commit flushes and any new pointer/key
  // event (the only way onCommit can fire) reaches the DOM. onDrag/onNudge's
  // own direct ref writes are unaffected: they run in event handlers, not
  // render, and this effect just re-assigns the same value they already set.
  useEffect(() => { latest.current = width; }, [width]);
  // Plain closures, not useCallback: nothing downstream keys an effect or a
  // memoized child on these identities, and wrapping them fights the React
  // Compiler's dependency inference over ref reads for no benefit (it flags
  // "Existing memoization could not be preserved" on some ref-access shapes
  // even when the listed deps are correct).
  const panelWidth = () => panel.current?.getBoundingClientRect().width ?? 1200;
  const onDrag = (clientX: number) => {
    const rect = panel.current?.getBoundingClientRect();
    if (!rect) return;
    const next = clampWidth(rect.right - clientX, rect.width);
    latest.current = next;
    setWidth(next);
  };
  const onNudge = (delta: number) => {
    const next = clampWidth((latest.current ?? panelWidth() / 2) + delta, panelWidth());
    latest.current = next;
    setWidth(next);
  };
  const onCommit = () => { if (latest.current) writeSidebarPrefs(scope, { ...readSidebarPrefs(scope), sessionColumnWidth: latest.current }); };
  const style = (width ? { "--room-session-width": `${width}px` } : {}) as CSSProperties;
  return { width, style, onDrag, onNudge, onCommit, value: width ?? panelWidth() / 2, min: SESSION_COLUMN_MIN, max: panelWidth() * 0.8 };
}
