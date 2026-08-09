// Ported from upstream Paperclip's pages/GoalDetail.tsx data-fetching block
// (paperclip/ui/src/pages/GoalDetail.tsx) (MIT, © 2025 Paperclip AI).
// v1: goal + the company goal list (for parent + sub-goals) + projects (for
// linked-projects). Defers asset uploads, breadcrumb context, panel context.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Goal, Issue, IssueProject } from "@/components/teams/shared/types";

export interface UseGoalDetailResult {
  goal: Goal | undefined;
  /** All goals in the company — used to resolve the parent + sub-goals. */
  allGoals: Goal[];
  /** All projects in the company — used to resolve linked projects. */
  projects: IssueProject[];
  /** All tasks in the company — filtered by `goalId` for the Tasks section.
   * Upstream's `IssueFilters` has no `goalId`, so there is no server-side
   * filter to ask for; this mirrors how linked projects are already resolved. */
  issues: Issue[];
  /** True while the issues fetch is in flight. Separate from `isLoading` so
   * the Tasks section can tell "not loaded yet" from "genuinely empty". */
  issuesLoading: boolean;
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

type GoalsResponse = { goals: Goal[] } | Goal[];
type ProjectsResponse = { projects: IssueProject[] } | IssueProject[];
type IssuesResponse = { issues: Issue[] } | Issue[];

function normalizeGoals(data: GoalsResponse | undefined): Goal[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.goals ?? [];
}

function normalizeProjects(data: ProjectsResponse | undefined): IssueProject[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.projects ?? [];
}

function normalizeIssues(data: IssuesResponse | undefined): Issue[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.issues ?? [];
}

export function useGoalDetail(goalId: string): UseGoalDetailResult {
  const { read } = useTeamsApi();
  const goal = read<Goal>(teamsQueryKeys.goals.detail(goalId));
  // The company goal list resolves the parent goal + the sub-goals tree
  // (upstream filters allGoals by parentId === goalId for the children).
  const goals = read<GoalsResponse>(teamsQueryKeys.goals.list());
  const projects = read<ProjectsResponse>(teamsQueryKeys.projects());
  // Unfiltered on purpose: a routine can carry a goalId, so its spawned tasks
  // belong on the goal page even though the Tasks panel hides them by default.
  const issues = read<IssuesResponse>("/issues");

  return {
    goal: goal.data,
    allGoals: normalizeGoals(goals.data),
    projects: normalizeProjects(projects.data),
    issues: normalizeIssues(issues.data),
    // Reported separately rather than folded into `isLoading`. The page's
    // full-view spinner is gated on `isLoading && !goal`, so once the (fast,
    // single-row) goal fetch lands the page renders no matter what the
    // aggregate says — folding this in would look like a fix and change
    // nothing. The Tasks section reads this directly so it can say "Loading…"
    // instead of "No tasks." while the (whole-company) issues fetch is in
    // flight; `normalizeIssues(undefined)` is `[]`, which is indistinguishable
    // from a genuinely empty result.
    issuesLoading: issues.isLoading,
    isLoading: goal.isLoading || goals.isLoading || projects.isLoading,
    isError: !!goal.error,
    error: goal.error || null,
  };
}
