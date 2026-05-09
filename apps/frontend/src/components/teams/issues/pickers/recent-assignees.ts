// localStorage-backed tracker for recently-picked assignee agent ids.
// Mirrors paperclip/ui/src/lib/recent-assignees.ts (MIT, © 2025 Paperclip AI).
// Used by AssigneePicker to bubble up frequently-picked agents.

export const RECENT_ASSIGNEES_KEY = "isol8.teams.recent-assignees";
const MAX_RECENT = 5;

function isBrowser(): boolean {
  try {
    return (
      typeof window !== "undefined" &&
      typeof window.localStorage !== "undefined" &&
      typeof window.localStorage.getItem === "function"
    );
  } catch {
    // SecurityError thrown in some sandboxed environments when accessing localStorage.
    return false;
  }
}

/** Returns most-recent-first, capped at 5. Empty on SSR or corrupted storage. */
export function getRecentAssignees(): string[] {
  if (!isBrowser()) return [];
  const raw = localStorage.getItem(RECENT_ASSIGNEES_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      localStorage.removeItem(RECENT_ASSIGNEES_KEY);
      return [];
    }
    return parsed.filter((x): x is string => typeof x === "string");
  } catch {
    // Defensive: storage corrupted by a different version of the code or
    // a foreign tab. Clear it so we don't keep choking on the same value.
    localStorage.removeItem(RECENT_ASSIGNEES_KEY);
    return [];
  }
}

/** Add `id` to the front, dedupe, cap at 5. No-op on SSR. Best-effort:
 * a localStorage write failure (quota exceeded, blocked storage in Safari
 * Private Browsing, sandboxed iframes, etc.) is swallowed so the caller's
 * subsequent ``onChange`` still runs. Recent-bubble-up is a UX nicety —
 * never block an assignment on persistence (Codex P2 on PR #575). */
export function trackRecentAssignee(id: string): void {
  if (!isBrowser()) return;
  const current = getRecentAssignees().filter((x) => x !== id);
  current.unshift(id);
  const next = current.slice(0, MAX_RECENT);
  try {
    localStorage.setItem(RECENT_ASSIGNEES_KEY, JSON.stringify(next));
  } catch {
    // Storage write failed — best-effort, don't block the picker.
  }
}
