// apps/frontend/src/components/teams/shared/lib/subIssues.ts
// Progress summary ported from upstream Paperclip's issue-detail-subissues.ts
// (paperclip/ui/src/lib/issue-detail-subissues.ts) (MIT, (c) 2025 Paperclip AI),
// adapted to our richer IssueStatus. Sort is the blockedBy-less reduction of
// upstream workflowSort (created-order); swap in the topo-sort when blockedBy
// (includeBlockedBy) lands.

import type { Issue, IssueStatus } from "@/components/teams/shared/types";

export type SubIssueProgressTargetKind = "next" | "blocked";
export interface SubIssueProgressTarget {
  issue: Issue;
  kind: SubIssueProgressTargetKind;
}
export interface SubIssueProgressSummary {
  totalCount: number;
  /** Literally `done`. The sub-issue strip's meaning of finished. */
  doneCount: number;
  /**
   * Everything FINISHED, not just `done`: also `won_t_do`, `duplicate` and
   * `closed` (cancelled is already out of `totalCount`). A goal heading needs
   * this one — counting only `done` means a goal whose last task was closed
   * reads "0 of 2 done" forever and can never reach its own total.
   */
  settledCount: number;
  inProgressCount: number;
  blockedCount: number;
  target: SubIssueProgressTarget | null;
}

// Our IssueStatus is broader than upstream's; treat all clearly-finished
// states as terminal so they're never picked as the "next" actionable target.
const TERMINAL_STATUSES = new Set<IssueStatus>([
  "done",
  "cancelled",
  "won_t_do",
  "duplicate",
  "closed",
]);

export function isTerminalStatus(status: IssueStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}
function isActionableStatus(status: IssueStatus): boolean {
  return !isTerminalStatus(status) && status !== "blocked";
}

function toTime(value: string | undefined): number {
  const t = value ? new Date(value).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

/** createdAt ascending, then id — the blockedBy-less reduction of workflowSort. */
export function sortSubIssues(issues: Issue[]): Issue[] {
  return [...issues].sort((a, b) => {
    const ta = toTime(a.createdAt ?? undefined);
    const tb = toTime(b.createdAt ?? undefined);
    if (ta !== tb) return ta - tb;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

export function buildSubIssueProgressSummary(issues: Issue[]): SubIssueProgressSummary {
  const progress = issues.filter((i) => i.status !== "cancelled");
  const ordered = sortSubIssues(progress);
  const count = (status: IssueStatus) => progress.filter((i) => i.status === status).length;

  const next = ordered.find((i) => isActionableStatus(i.status)) ?? null;
  const remaining = ordered.filter((i) => !isTerminalStatus(i.status));
  const blocked =
    next === null && remaining.length > 0 && remaining.every((i) => i.status === "blocked")
      ? remaining[0]
      : null;

  return {
    totalCount: progress.length,
    doneCount: count("done"),
    settledCount: progress.filter((i) => isTerminalStatus(i.status)).length,
    inProgressCount: count("in_progress"),
    blockedCount: count("blocked"),
    target: next ? { issue: next, kind: "next" } : blocked ? { issue: blocked, kind: "blocked" } : null,
  };
}
