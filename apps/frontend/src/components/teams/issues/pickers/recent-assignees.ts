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

// Both read and write are best-effort. localStorage.getItem /
// .setItem / .removeItem can ALL throw in private/sandboxed
// environments even when the localStorage object itself exists
// (Safari Private Browsing pre-15, quota exceeded, sandboxed
// iframes, COEP-restricted contexts). isBrowser() is necessary but
// not sufficient — we need defensive try/catch on every storage
// call too. AssigneePicker treats this util as a UX nicety; never
// let it block the actual assignment PATCH.

/** Returns most-recent-first, capped at 5. Empty on SSR or any storage failure. */
export function getRecentAssignees(): string[] {
  if (!isBrowser()) return [];
  let raw: string | null;
  try {
    raw = localStorage.getItem(RECENT_ASSIGNEES_KEY);
  } catch {
    // Storage read blocked. Best-effort: pretend empty.
    return [];
  }
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      safeRemove();
      return [];
    }
    return parsed.filter((x): x is string => typeof x === "string");
  } catch {
    // Storage corrupted (different code version, foreign tab, etc.).
    safeRemove();
    return [];
  }
}

/** Add `id` to the front, dedupe, cap at 5. No-op on SSR or any storage failure. */
export function trackRecentAssignee(id: string): void {
  if (!isBrowser()) return;
  const current = getRecentAssignees().filter((x) => x !== id);
  current.unshift(id);
  const next = current.slice(0, MAX_RECENT);
  try {
    localStorage.setItem(RECENT_ASSIGNEES_KEY, JSON.stringify(next));
  } catch {
    // Storage write blocked — best-effort.
  }
}

/** removeItem can throw in the same scenarios as getItem/setItem; never let
 * the corruption-recovery path itself throw and block the caller. */
function safeRemove(): void {
  try {
    localStorage.removeItem(RECENT_ASSIGNEES_KEY);
  } catch {
    // Storage delete blocked — give up, return [] anyway.
  }
}
