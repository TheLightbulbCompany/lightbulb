// apps/frontend/src/components/teams/shared/lib/inbox.ts

// Ported (subset) from upstream Paperclip's lib/inbox.ts
// (paperclip/ui/src/lib/inbox.ts) (MIT, (c) 2025 Paperclip AI).
// Subset: pure helpers only. localStorage helpers in inboxStorage.ts.
//
// The Inbox page no longer renders issue rows, so what remains here is what
// the landing page's WorkspaceDemo still uses to render its marketing
// inbox — the per-tab reads, the keyboard-nav projection and the "mine"
// status filter went with the pill tabs.

import type { Issue } from "@/components/teams/shared/types";

// Re-export so consumers can `import { InboxTab } from ".../lib/inbox"` without
// reaching into queryKeys.ts. The canonical type lives in queryKeys.ts.
import type { InboxTab } from "@/components/teams/shared/queryKeys";
export type { InboxTab };

// Re-export the column constants from inboxColumns.ts so call sites can pull
// everything inbox-shaped through this module.
export {
  inboxIssueColumns,
  DEFAULT_INBOX_ISSUE_COLUMNS,
  type InboxIssueColumn,
} from "@/components/teams/shared/lib/inboxColumns";

/**
 * v1 discriminated union — only the `issue` variant is used. Approval /
 * failed-run / join-request variants are deferred per the #3c plan and will
 * be added when the corresponding inbox rows ship.
 */
export type InboxWorkItem = {
  kind: "issue";
  issue: Issue;
};

/**
 * Simpler than upstream's `InboxGroupedSection` (no nesting, no workspace
 * groups, no childrenByIssueId). v1 only splits Today vs Earlier and a
 * single search section.
 */
export interface InboxGroupedSection {
  kind: "today" | "earlier" | "search";
  items: InboxWorkItem[];
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/**
 * Wrap each issue in a `{ kind: "issue", issue }` entry. Trivial in v1; the
 * full upstream version interleaves approvals / runs / joins with timestamp
 * sorting.
 */
export function getInboxWorkItems(issues: Issue[]): InboxWorkItem[] {
  return issues.map((issue) => ({ kind: "issue" as const, issue }));
}

/**
 * Case-insensitive substring match against an issue's `title` and `identifier`.
 * Returns `true` for an empty / whitespace-only query (no filter applied).
 */
export function matchesInboxIssueSearch(issue: Issue, query: string): boolean {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) return true;
  if (issue.title?.toLowerCase().includes(normalizedQuery)) return true;
  if (issue.identifier?.toLowerCase().includes(normalizedQuery)) return true;
  return false;
}

const TWENTY_FOUR_HOURS_MS = 24 * 60 * 60 * 1000;

function isWithinLast24h(issueIso: string | null | undefined, cutoffMs: number): boolean {
  if (!issueIso) return false;
  const t = new Date(issueIso).getTime();
  if (!Number.isFinite(t)) return false;
  return t >= cutoffMs;
}

/**
 * Build the grouped sections rendered by `<InboxList />`.
 *
 * - When `searchQuery` is non-empty, returns a single `{ kind: "search" }`
 *   section containing all items (search-filtering happens upstream of this
 *   call).
 * - Otherwise splits items by `issue.updatedAt` into Today (within the last
 *   24h) and Earlier sections. Empty sections are omitted.
 *
 * Uses a rolling 24h window (not local-midnight) so the boundary is timezone-
 * agnostic — matches upstream Paperclip's `Inbox.tsx` (`Date.now() - 24h`).
 *
 * `nowIso` is required so callers can drive the date boundary deterministically
 * in tests.
 */
export function buildGroupedInboxSections(
  items: InboxWorkItem[],
  options: { searchQuery?: string; nowIso: string },
): InboxGroupedSection[] {
  const searchQuery = options.searchQuery?.trim() ?? "";
  if (searchQuery) {
    if (items.length === 0) return [];
    return [{ kind: "search", items }];
  }

  const nowMs = new Date(options.nowIso).getTime();
  if (!Number.isFinite(nowMs)) {
    throw new Error(`buildGroupedInboxSections: invalid nowIso ${JSON.stringify(options.nowIso)}`);
  }
  const todayCutoffMs = nowMs - TWENTY_FOUR_HOURS_MS;
  const today: InboxWorkItem[] = [];
  const earlier: InboxWorkItem[] = [];
  for (const item of items) {
    const ts = item.issue.updatedAt ?? item.issue.lastActivityAt ?? null;
    if (isWithinLast24h(ts, todayCutoffMs)) {
      today.push(item);
    } else {
      earlier.push(item);
    }
  }

  const sections: InboxGroupedSection[] = [];
  if (today.length > 0) sections.push({ kind: "today", items: today });
  if (earlier.length > 0) sections.push({ kind: "earlier", items: earlier });
  return sections;
}
