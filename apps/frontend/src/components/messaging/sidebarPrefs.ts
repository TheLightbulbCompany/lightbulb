import { useCallback, useState, type CSSProperties, type RefObject } from "react";
import type { SidebarSort } from "./roomSections";

const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort; sessionColumnWidth?: number }
const EMPTY: SidebarPrefs = { collapsed: [], sort: "alpha" };

export function readSidebarPrefs(scope: string): SidebarPrefs {
  try {
    const raw = window.localStorage.getItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<SidebarPrefs>;
    return {
      collapsed: Array.isArray(parsed.collapsed) ? parsed.collapsed.filter((value) => typeof value === "string") : [],
      sort: parsed.sort === "unread" ? "unread" : "alpha",
      ...(typeof parsed.sessionColumnWidth === "number" && Number.isFinite(parsed.sessionColumnWidth) && parsed.sessionColumnWidth >= 360
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

export const SESSION_COLUMN_MIN = 360;
const clampWidth = (px: number, panelWidth: number) => Math.min(Math.max(px, SESSION_COLUMN_MIN), Math.round(panelWidth * 0.8));

/** Width of the session column for both placements, persisted per workspace. */
export function useSessionColumnWidth(scope: string, panel: RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState<number | undefined>(() => (typeof window === "undefined" ? undefined : readSidebarPrefs(scope).sessionColumnWidth));
  const panelWidth = useCallback(() => panel.current?.getBoundingClientRect().width ?? 1200, [panel]);
  const onDrag = useCallback((clientX: number) => {
    const rect = panel.current?.getBoundingClientRect();
    if (rect) setWidth(clampWidth(rect.right - clientX, rect.width));
  }, [panel]);
  const onNudge = useCallback((delta: number) => setWidth((current) => clampWidth((current ?? panelWidth() / 2) + delta, panelWidth())), [panelWidth]);
  const onCommit = useCallback(() => { if (width) writeSidebarPrefs(scope, { ...readSidebarPrefs(scope), sessionColumnWidth: width }); }, [scope, width]);
  const style = (width ? { "--room-session-width": `${width}px` } : {}) as CSSProperties;
  return { width, style, onDrag, onNudge, onCommit };
}
