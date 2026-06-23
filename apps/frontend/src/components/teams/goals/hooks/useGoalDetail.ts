// Ported from upstream Paperclip's pages/GoalDetail.tsx data-fetching block
// (paperclip/ui/src/pages/GoalDetail.tsx) (MIT, © 2025 Paperclip AI).
// v1: goal + the company goal list (for parent + sub-goals) + projects (for
// linked-projects). Defers asset uploads, breadcrumb context, panel context.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Goal, IssueProject } from "@/components/teams/shared/types";

export interface UseGoalDetailResult {
  goal: Goal | undefined;
  /** All goals in the company — used to resolve the parent + sub-goals. */
  allGoals: Goal[];
  /** All projects in the company — used to resolve linked projects. */
  projects: IssueProject[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

type GoalsResponse = { goals: Goal[] } | Goal[];
type ProjectsResponse = { projects: IssueProject[] } | IssueProject[];

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

export function useGoalDetail(goalId: string): UseGoalDetailResult {
  const { read } = useTeamsApi();
  const goal = read<Goal>(teamsQueryKeys.goals.detail(goalId));
  // The company goal list resolves the parent goal + the sub-goals tree
  // (upstream filters allGoals by parentId === goalId for the children).
  const goals = read<GoalsResponse>(teamsQueryKeys.goals.list());
  const projects = read<ProjectsResponse>(teamsQueryKeys.projects());

  return {
    goal: goal.data,
    allGoals: normalizeGoals(goals.data),
    projects: normalizeProjects(projects.data),
    isLoading: goal.isLoading || goals.isLoading || projects.isLoading,
    isError: !!goal.error,
    error: goal.error || null,
  };
}
