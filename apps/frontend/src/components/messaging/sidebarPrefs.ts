import type { SessionMarks } from "@lightbulb/shared";
import type { SidebarSort } from "./roomSections";
import type { SessionGroupBy, SessionShow } from "./sessionGroups";

const SIDEBAR_PREFS_KEY_PREFIX = "lightbulb.messaging.sidebar.";
export interface SidebarPrefs { collapsed: string[]; sort: SidebarSort; sessionGroupBy: SessionGroupBy; sessionShow: SessionShow; sessionsExpanded: string[] }
const EMPTY: SidebarPrefs = { collapsed: [], sort: "recent", sessionGroupBy: "person", sessionShow: "all", sessionsExpanded: [] };
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
      sessionGroupBy: parsed.sessionGroupBy === "state" || parsed.sessionGroupBy === "repo" ? parsed.sessionGroupBy : "person",
      sessionShow: parsed.sessionShow === "mine" || parsed.sessionShow === "live" ? parsed.sessionShow : "all",
      sessionsExpanded: strings(parsed.sessionsExpanded),
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

const SESSION_MARKS_KEY_PREFIX = "lightbulb.messaging.sessionMarks.";

/** Per viewer, on this browser: pinned and archived sessions (`SessionMarks` in shared). `key` is
 *  `<userId>:<workspace>`, so people sharing a browser profile do not share marks. Nothing server-side. */
export function readSessionMarks(scope: string): SessionMarks {
  try {
    const parsed: Partial<SessionMarks> = JSON.parse(window.localStorage.getItem(`${SESSION_MARKS_KEY_PREFIX}${scope}`) ?? "{}") ?? {};
    const archived = parsed.archived && typeof parsed.archived === "object" && !Array.isArray(parsed.archived) ? parsed.archived : {};
    return {
      pinned: Array.isArray(parsed.pinned) ? parsed.pinned.filter((id) => typeof id === "string") : [],
      archived: Object.fromEntries(Object.entries(archived).filter(([, seq]) => typeof seq === "number")),
    };
  } catch {
    return { pinned: [], archived: {} };
  }
}

export function writeSessionMarks(scope: string, marks: SessionMarks): void {
  try {
    window.localStorage.setItem(`${SESSION_MARKS_KEY_PREFIX}${scope}`, JSON.stringify(marks));
  } catch {
    // Blocked storage: pins and archives last until the page reloads.
  }
}
