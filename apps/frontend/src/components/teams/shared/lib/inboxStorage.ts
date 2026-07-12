// apps/frontend/src/components/teams/shared/lib/inboxStorage.ts

// Ported from upstream Paperclip's lib/inbox.ts (loadInbox*/saveInbox*) and
// hooks/useInboxBadge.ts (loadReadInboxItems/saveReadInboxItems).
// (paperclip/ui/src/lib/inbox.ts, hooks/useInboxBadge.ts) (MIT, (c) 2025 Paperclip AI).

// Deliberate divergences from upstream:
//   - Key shape `paperclip:inbox:<companyId>:<key>` (vs upstream's
//     `paperclip:inbox:<key>:<companyId>`) — sidesteps cross-tenant bleed
//     latent in upstream's per-key-prefix scheme. No upstream prefs to
//     migrate since this is a fresh BFF surface.

import type { InboxFilterPreferences } from "@/components/teams/shared/lib/inbox";
import type { InboxTab } from "@/components/teams/shared/queryKeys";
import { defaultIssueFilterState } from "@/components/teams/shared/lib/issueFilters";

// Source of truth for runtime tab validation. The Record<InboxTab, true>
// shape forces a TS error if the InboxTab union grows without updating
// this map, so `loadLastInboxTab` can never silently accept a stale value.
const KNOWN_INBOX_TABS_MAP: Record<InboxTab, true> = {
  mine: true,
  recent: true,
  all: true,
  unread: true,
  approvals: true,
  runs: true,
  joins: true,
};
const DEFAULT_INBOX_TAB: InboxTab = "mine";

/**
 * Runtime InboxTab validation for external inputs (?tab deep links, threaded
 * panel params). Own-property check — `in` would walk the prototype chain and
 * accept "constructor" et al.
 */
export function isInboxTab(value: string | null | undefined): value is InboxTab {
  return (
    typeof value === "string" &&
    Object.prototype.hasOwnProperty.call(KNOWN_INBOX_TABS_MAP, value)
  );
}

/**
 * Build the namespaced localStorage key for a per-company inbox preference.
 * Exported for tests + cross-tab `storage` event filtering.
 */
export function inboxStorageKey(companyId: string, key: string): string {
  return `paperclip:inbox:${companyId}:${key}`;
}

function buildDefaultFilterPreferences(
  defaults?: Partial<InboxFilterPreferences>,
): InboxFilterPreferences {
  return {
    issueFilters: { ...defaultIssueFilterState, ...(defaults?.issueFilters ?? {}) },
  };
}

/**
 * Read the per-company inbox filter preferences from localStorage. Returns
 * `defaults` (merged with `defaultIssueFilterState`) when the key is missing,
 * the JSON is malformed, or `localStorage` access throws (private browsing).
 */
export function loadInboxFilterPreferences(
  companyId: string,
  defaults?: Partial<InboxFilterPreferences>,
): InboxFilterPreferences {
  const fallback = buildDefaultFilterPreferences(defaults);
  let raw: string | null;
  try {
    raw = localStorage.getItem(inboxStorageKey(companyId, "filters"));
  } catch {
    return fallback;
  }
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw) as Partial<InboxFilterPreferences> | null;
    if (!parsed || typeof parsed !== "object") return fallback;
    return {
      issueFilters: {
        ...defaultIssueFilterState,
        ...(defaults?.issueFilters ?? {}),
        ...(parsed.issueFilters ?? {}),
      },
    };
  } catch {
    return fallback;
  }
}

/** Persist the per-company inbox filter preferences. Swallows storage errors. */
export function saveInboxFilterPreferences(
  companyId: string,
  prefs: InboxFilterPreferences,
): void {
  try {
    localStorage.setItem(inboxStorageKey(companyId, "filters"), JSON.stringify(prefs));
  } catch {
    // Ignore localStorage failures (private browsing, quota, etc.).
  }
}

/** Persist the last-active inbox tab for this company. Swallows storage errors. */
export function saveLastInboxTab(companyId: string, tab: InboxTab): void {
  try {
    localStorage.setItem(inboxStorageKey(companyId, "tab"), tab);
  } catch {
    // Ignore localStorage failures.
  }
}

/**
 * Read + validate the last-active inbox tab. Returns `"mine"` (the upstream
 * default) when the key is missing, the stored value isn't a known InboxTab,
 * or storage access throws — matches upstream behavior at
 * paperclip/ui/src/lib/inbox.ts:614-631.
 */
export function loadLastInboxTab(companyId: string): InboxTab {
  let raw: string | null;
  try {
    raw = localStorage.getItem(inboxStorageKey(companyId, "tab"));
  } catch {
    return DEFAULT_INBOX_TAB;
  }
  if (!raw) return DEFAULT_INBOX_TAB;
  return raw in KNOWN_INBOX_TABS_MAP ? (raw as InboxTab) : DEFAULT_INBOX_TAB;
}

/**
 * Read the set of read-marked inbox item IDs for this company. Returns an
 * empty Set when the key is missing, the JSON is malformed, or storage access
 * throws.
 */
export function loadReadInboxItems(companyId: string): Set<string> {
  let raw: string | null;
  try {
    raw = localStorage.getItem(inboxStorageKey(companyId, "read-items"));
  } catch {
    return new Set();
  }
  if (!raw) return new Set();
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((value): value is string => typeof value === "string"));
  } catch {
    return new Set();
  }
}

/** Persist the read-marked inbox item IDs as a JSON array. Swallows storage errors. */
export function saveReadInboxItems(companyId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(
      inboxStorageKey(companyId, "read-items"),
      JSON.stringify([...ids]),
    );
  } catch {
    // Ignore localStorage failures.
  }
}

/**
 * Read the per-company set of acknowledged (dismissed-from-band) failed-run
 * IDs. Mirrors loadReadInboxItems. Empty Set on missing/malformed/throw.
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
    // Ignore localStorage failures.
  }
}

/** Read the per-company "Needs attention" band collapsed flag. Default false. */
export function loadBandCollapsed(companyId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return localStorage.getItem(inboxStorageKey(companyId, "triage-collapsed")) === "1";
  } catch {
    return false;
  }
}

/** Persist the band collapsed flag. Swallows storage errors. */
export function saveBandCollapsed(companyId: string, collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(
      inboxStorageKey(companyId, "triage-collapsed"),
      collapsed ? "1" : "0",
    );
  } catch {
    // Swallow — Safari Private mode / quota exceeded.
  }
}
