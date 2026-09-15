import { useRef, useState, type CSSProperties, type RefObject } from "react";
import type { SidebarSort } from "./roomSections";

const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort }
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

// Its own key, not a field inside SidebarPrefs -- the width is written from a
// drag/nudge gesture and the rest of the blob from sort/collapse toggles, and
// sharing one key meant every writer had to read-merge-write around the other.
const SESSION_COLUMN_KEY_PREFIX = "lightbulb.messaging.sessionColumnWidth.";

export function readSessionColumnWidth(scope: string): number | undefined {
  try {
    const raw = window.localStorage.getItem(`${SESSION_COLUMN_KEY_PREFIX}${scope}`);
    if (!raw) return undefined;
    const width = Number(raw);
    return Number.isFinite(width) && width >= SESSION_COLUMN_MIN ? width : undefined;
  } catch {
    return undefined;
  }
}

export function writeSessionColumnWidth(scope: string, width: number): void {
  try {
    window.localStorage.setItem(`${SESSION_COLUMN_KEY_PREFIX}${scope}`, String(width));
  } catch {
    // Private windows and blocked site data: the sidebar simply forgets.
  }
}

/** Width of the session column for both placements, persisted per workspace.
 *  Callers remount this hook on a scope change (RoomPanel's RoomBoundary key,
 *  SessionFocus's `key={scope}`), so it only ever needs to read `scope` once. */
export function useSessionColumnWidth(scope: string, panel: RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState<number | undefined>(() => (typeof window === "undefined" ? undefined : readSessionColumnWidth(scope)));
  // `onCommit` must persist the width from the MOST RECENT nudge/drag, not the
  // `width` state closed over at the render that handed ColumnGrip its props --
  // a keyboard nudge calls onNudge() then onCommit() in the same handler,
  // before React has re-rendered with the new state. A ref is always current.
  // `latest` is kept current by onDrag/onNudge themselves (each sets it before
  // setWidth), so there is nothing left for a width-sync effect to do -- the
  // ref and the state it mirrors always change together, in the same handler.
  const latest = useRef(width);
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
  const onCommit = () => { if (latest.current) writeSessionColumnWidth(scope, latest.current); };
  const style = (width ? { "--room-session-width": `${width}px` } : {}) as CSSProperties;
  return {
    style,
    grip: { onDrag, onNudge, onCommit, value: width ?? panelWidth() / 2, min: SESSION_COLUMN_MIN, max: panelWidth() * 0.8 },
  };
}
