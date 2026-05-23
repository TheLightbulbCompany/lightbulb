// Ported from upstream Paperclip's inbox.ts (paperclip/ui/src/lib/inbox.ts)
// (MIT, (c) 2025 Paperclip AI). Slim subset retained for IssueColumns: only
// the column-key tuple/type and the default-visible-columns constant. The
// full upstream module also contains localStorage column persistence,
// availability helpers, and workspace-name resolution — those are not yet
// ported.

export const inboxIssueColumns = [
  "status",
  "id",
  "assignee",
  "project",
  "workspace",
  "parent",
  "labels",
  "updated",
] as const;

export type InboxIssueColumn = (typeof inboxIssueColumns)[number];

export const DEFAULT_INBOX_ISSUE_COLUMNS: InboxIssueColumn[] = [
  "status",
  "id",
  "updated",
];
