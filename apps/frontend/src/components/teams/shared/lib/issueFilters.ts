// apps/frontend/src/components/teams/shared/lib/issueFilters.ts

// Ported from upstream Paperclip's issue-filters.ts
// (paperclip/ui/src/lib/issue-filters.ts) (MIT, (c) 2025 Paperclip AI).
// Subset retained for IssueFiltersPopover — workspace resolution, apply /
// count helpers, and the Issue-typed application logic stay upstream
// (they're only used by IssuesList which is out of scope for this PR).

import type { IssuePriority, IssueStatus } from "@/components/teams/shared/types";

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
