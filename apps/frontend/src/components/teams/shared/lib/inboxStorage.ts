// apps/frontend/src/components/teams/shared/lib/inboxStorage.ts

// Ported from upstream Paperclip's lib/inbox.ts and hooks/useInboxBadge.ts
// (paperclip/ui/src/lib/inbox.ts, hooks/useInboxBadge.ts)
// (MIT, (c) 2025 Paperclip AI).
//
// What is left here is the ONE per-browser preference the Inbox still keeps:
// which failed runs this person has dismissed. The last-tab, read-marks, band-
// collapsed and filter-preference stores went with the pill tabs and the
// issue list — Inbox no longer has tabs, rows, or read state to remember.
//
// Deliberate divergence from upstream: key shape
// `paperclip:inbox:<companyId>:<key>` (vs upstream's
// `paperclip:inbox:<key>:<companyId>`) — sidesteps cross-tenant bleed latent
// in upstream's per-key-prefix scheme.

/**
 * Build the namespaced localStorage key for a per-company inbox preference.
 * Exported for tests + cross-tab `storage` event filtering.
 */
export function inboxStorageKey(companyId: string, key: string): string {
  return `paperclip:inbox:${companyId}:${key}`;
}

/**
 * Read the per-company set of acknowledged (dismissed-from-band) failed-run
 * IDs. Empty Set on missing/malformed/throw.
 */
export function loadAckRuns(companyId: string): Set<string> {
  let raw: string | null;
  try {
    raw = localStorage.getItem(inboxStorageKey(companyId, "ack-runs"));
  } catch {
    return new Set();
  }
  if (!raw) return new Set();
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((v): v is string => typeof v === "string"));
  } catch {
    return new Set();
  }
}

/** Persist the acknowledged failed-run IDs as a JSON array. Swallows errors. */
export function saveAckRuns(companyId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(
      inboxStorageKey(companyId, "ack-runs"),
      JSON.stringify([...ids]),
    );
  } catch {
    // Ignore localStorage failures (private browsing, quota, etc.).
  }
}
