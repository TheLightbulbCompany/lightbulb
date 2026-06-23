// apps/frontend/src/components/teams/projects/hooks/useProjectDetail.ts

// Ported from upstream Paperclip's pages/ProjectDetail.tsx data-fetching block
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors useGoalDetail.ts: the project row + its project-scoped issues (the
// Issues tab). The Issues tab fetch uses the BFF `?project=` filter (PR #1006).
// Defers upstream's budget-overview / live-runs / workspace queries.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Project, Issue } from "@/components/teams/shared/types";

export interface UseProjectDetailResult {
  project: Project | undefined;
  /** Issues whose `projectId` matches this project (the Issues tab). */
  issues: Issue[];
  isLoading: boolean;
  /** Separate loading flag for the issues list so the header renders first. */
  issuesLoading: boolean;
  isError: boolean;
  error: Error | null;
}

type IssuesResponse = { issues: Issue[] } | Issue[];

function normalizeIssues(data: IssuesResponse | undefined): Issue[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.issues ?? [];
}

export function useProjectDetail(projectId: string): UseProjectDetailResult {
  const { read } = useTeamsApi();
  const project = read<Project>(teamsQueryKeys.projectDetail(projectId));
  // Project-scoped issues for the Issues tab — the BFF forwards `?project=` to
  // upstream's `projectId` filter (PR #1006). Only the project read gates
  // `isError`; an issues-list failure renders an empty Issues tab, not a
  // whole-page error (mirrors useGoalDetail's projects-error handling).
  const issues = read<IssuesResponse>(teamsQueryKeys.issuesByProject(projectId));

  return {
    project: project.data,
    issues: normalizeIssues(issues.data),
    isLoading: project.isLoading,
    issuesLoading: issues.isLoading,
    isError: !!project.error,
    error: project.error || null,
  };
}
