// apps/frontend/src/components/teams/shared/lib/issueFilters.ts

// Ported from upstream Paperclip's issue-filters.ts
// (paperclip/ui/src/lib/issue-filters.ts) (MIT, (c) 2025 Paperclip AI).
// Subset retained for IssueFiltersPopover plus the `applyIssueFilters`
// predicate — the apply logic is adapted to the slim Teams `Issue` shape
// (which carries `labels`/`project` objects, not upstream's flat
// `labelIds`/`projectId`), so TasksList and InboxPage filter identically
// off a single source of truth instead of each rolling its own predicate.

import type {
  Issue,
  IssuePriority,
  IssueStatus,
} from "@/components/teams/shared/types";

export type IssueFilterState = {
  statuses: string[];
  priorities: string[];
  assignees: string[];
  creators: string[];
  labels: string[];
  projects: string[];
  workspaces: string[];
  liveOnly?: boolean;
  hideRoutineExecutions: boolean;
};

export const defaultIssueFilterState: IssueFilterState = {
  statuses: [],
  priorities: [],
  assignees: [],
  creators: [],
  labels: [],
  projects: [],
  workspaces: [],
  liveOnly: false,
  hideRoutineExecutions: false,
};

export const issueStatusOrder: IssueStatus[] = [
  "in_progress",
  "todo",
  "backlog",
  "in_review",
  "blocked",
  "done",
  "cancelled",
];
export const issuePriorityOrder: IssuePriority[] = ["critical", "high", "medium", "low"];

export const issueQuickFilterPresets = [
  { label: "All", statuses: [] as string[] },
  { label: "Active", statuses: ["todo", "in_progress", "in_review", "blocked"] },
  { label: "Backlog", statuses: ["backlog"] },
  { label: "Done", statuses: ["done", "cancelled"] },
];

export function issueFilterLabel(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

export function issueFilterArraysEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const sortedA = [...a].sort();
  const sortedB = [...b].sort();
  return sortedA.every((value, index) => value === sortedB[index]);
}

export function toggleIssueFilterValue(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((existing) => existing !== value) : [...values, value];
}

// Count of distinct active filter *facets* (not the sum of selected values) —
// matches the badge upstream shows on the popover trigger. The InboxToolbar's
// own `countActiveFilters` sums values; this facet count is what the Tasks
// toolbar surfaces, so keep them deliberately distinct.
export function countActiveIssueFilters(state: IssueFilterState): number {
  let count = 0;
  if (state.statuses.length > 0) count += 1;
  if (state.priorities.length > 0) count += 1;
  if (state.assignees.length > 0) count += 1;
  if (state.creators.length > 0) count += 1;
  if (state.labels.length > 0) count += 1;
  if (state.projects.length > 0) count += 1;
  if (state.workspaces.length > 0) count += 1;
  if (state.liveOnly) count += 1;
  return count;
}

// Apply the filter state to a slim-`Issue` array, client-side. Adapted from
// upstream `applyIssueFilters` to our type: labels/projects live on nested
// objects (`issue.labels[].id`, `issue.project.id`) rather than flat id
// arrays, and the slim type carries no `originKind`/`createdByAgentId`/
// `workspace` so the routine-visibility, agent-creator, and workspace facets
// are intentionally dropped. `liveOnly` needs the caller's live-id set (the
// same set that lights the row/board indicators).
export function applyIssueFilters(
  issues: Issue[],
  state: IssueFilterState,
  options: {
    currentUserId?: string | null;
    liveIssueIds?: ReadonlySet<string>;
  } = {},
): Issue[] {
  const { currentUserId, liveIssueIds } = options;
  let result = issues;

  if (state.liveOnly) {
    result = result.filter((issue) => liveIssueIds?.has(issue.id) === true);
  }
  if (state.statuses.length > 0) {
    result = result.filter((issue) => state.statuses.includes(issue.status));
  }
  if (state.priorities.length > 0) {
    result = result.filter(
      (issue) => issue.priority != null && state.priorities.includes(issue.priority),
    );
  }
  if (state.assignees.length > 0) {
    result = result.filter((issue) => {
      for (const assignee of state.assignees) {
        if (assignee === "__unassigned" && !issue.assigneeAgentId && !issue.assigneeUserId)
          return true;
        if (assignee === "__me" && currentUserId && issue.assigneeUserId === currentUserId)
          return true;
        if (issue.assigneeAgentId === assignee) return true;
      }
      return false;
    });
  }
  if (state.creators.length > 0) {
    result = result.filter((issue) => {
      for (const creator of state.creators) {
        if (creator.startsWith("user:") && issue.createdByUserId === creator.slice("user:".length))
          return true;
      }
      return false;
    });
  }
  if (state.labels.length > 0) {
    result = result.filter((issue) =>
      (issue.labels ?? []).some((label) => state.labels.includes(label.id)),
    );
  }
  if (state.projects.length > 0) {
    result = result.filter(
      (issue) => issue.project != null && state.projects.includes(issue.project.id),
    );
  }
  return result;
}
