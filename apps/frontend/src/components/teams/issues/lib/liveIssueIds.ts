// apps/frontend/src/components/teams/issues/lib/liveIssueIds.ts

// Ported from upstream Paperclip's ui/src/lib/liveIssueIds.ts
// @ v2026.1012.0-isol8.4 (MIT, (c) 2025 Paperclip AI). collectLiveIssueIds and
// its helpers are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - api/heartbeats LiveRunForIssue -> shared/lib/liveRuns LiveRun (the
//                                       /inbox/live-runs row useLiveRuns returns)
//   - @paperclipai/shared IssueStatus -> a plain string set
//   - collectSubtreeLiveCounts is not ported (no tree view consumes it here)

import type { LiveRun } from "@/components/teams/shared/lib/liveRuns";

function isLiveRunStatus(status: string | null | undefined): boolean {
  return status === "queued" || status === "running";
}

const TERMINAL_ISSUE_STATUSES = new Set<string>(["done", "cancelled"]);

export function isTerminalIssueStatus(status: string | null | undefined): boolean {
  return TERMINAL_ISSUE_STATUSES.has(status as string);
}

export function isLiveIssueRun(
  run: Pick<LiveRun, "status">,
  issueStatus?: string | null,
): boolean {
  return isLiveRunStatus(run.status) && !isTerminalIssueStatus(issueStatus);
}

export interface LiveIssueStatusNode {
  id: string;
  status: string;
  updatedAt?: Date | string | number | null;
}

function collectIssueStatusById(issues: readonly LiveIssueStatusNode[] | null | undefined): Map<string, string> {
  const snapshotByIssueId = new Map<string, { status: string; updatedAtMs: number | null }>();
  for (const issue of issues ?? []) {
    const candidate = {
      status: issue.status,
      updatedAtMs: issueUpdatedAtMs(issue.updatedAt),
    };
    const existing = snapshotByIssueId.get(issue.id);
    if (!existing || shouldReplaceIssueStatusSnapshot(existing, candidate)) snapshotByIssueId.set(issue.id, candidate);
  }
  return new Map([...snapshotByIssueId].map(([issueId, snapshot]) => [issueId, snapshot.status]));
}

function issueUpdatedAtMs(updatedAt: LiveIssueStatusNode["updatedAt"]): number | null {
  if (updatedAt === null || updatedAt === undefined) return null;
  const timestamp = updatedAt instanceof Date ? updatedAt.getTime() : new Date(updatedAt).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

function shouldReplaceIssueStatusSnapshot(
  existing: { status: string; updatedAtMs: number | null },
  candidate: { status: string; updatedAtMs: number | null },
): boolean {
  if (candidate.updatedAtMs !== null && existing.updatedAtMs !== null) {
    if (candidate.updatedAtMs !== existing.updatedAtMs) return candidate.updatedAtMs > existing.updatedAtMs;
  } else if (candidate.updatedAtMs !== null) {
    return true;
  } else if (existing.updatedAtMs !== null) {
    return false;
  }

  return !isTerminalIssueStatus(existing.status) && isTerminalIssueStatus(candidate.status);
}

export function collectLiveIssueIds(
  liveRuns: readonly LiveRun[] | null | undefined,
  issues?: readonly LiveIssueStatusNode[] | null,
): Set<string> {
  const ids = new Set<string>();
  const statusByIssueId = collectIssueStatusById(issues);
  for (const run of liveRuns ?? []) {
    if (run.issueId && isLiveIssueRun(run, statusByIssueId.get(run.issueId))) ids.add(run.issueId);
  }
  return ids;
}
