import type { SidebarSort } from "./roomSections";

const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort }
const EMPTY: SidebarPrefs = { collapsed: [], sort: "recent" };
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((v) => typeof v === "string") : [];

export function readSidebarPrefs(scope: string): SidebarPrefs {
  try {
    const raw = window.localStorage.getItem(`${SIDEBAR_PREFS_KEY_PREFIX}${scope}`);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<SidebarPrefs>;
    return {
      collapsed: strings(parsed.collapsed),
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

const SESSION_SEEN_KEY_PREFIX = "lightbulb.messaging.sessionSeen.";

/** Per-viewer "last seen seq" per shared session, for the sidebar's unread dot. `scope` is
 *  `<userId>:<workspace>`, so people sharing a browser profile do not share it. */
export function readSessionSeen(scope: string): Record<string, number> {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(`${SESSION_SEEN_KEY_PREFIX}${scope}`) ?? "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter(([, seq]) => typeof seq === "number"));
  } catch {
    return {};
  }
}

export function writeSessionSeen(scope: string, seen: Readonly<Record<string, number>>): void {
  try {
    window.localStorage.setItem(`${SESSION_SEEN_KEY_PREFIX}${scope}`, JSON.stringify(seen));
  } catch {
    // Blocked storage: every session simply re-baselines next load.
  }
}
