import type { SidebarSort } from "./roomSections";

const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort }
const EMPTY: SidebarPrefs = { collapsed: [], sort: "recent" };

export function readSidebarPrefs(scope: string): SidebarPrefs {
  try {
    const raw = window.localStorage.getItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<SidebarPrefs>;
    return {
      collapsed: Array.isArray(parsed.collapsed) ? parsed.collapsed.filter((value) => typeof value === "string") : [],
      // A stored "alpha" was the old default, written on any collapse toggle whether or not
      // anyone chose it -- so it reads as the new default; A–Z is now stored as "name".
      sort: parsed.sort === "unread" || parsed.sort === "name" ? parsed.sort : "recent",
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
