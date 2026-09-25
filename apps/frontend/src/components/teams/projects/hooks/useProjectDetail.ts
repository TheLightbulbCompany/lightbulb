// apps/frontend/src/components/teams/projects/hooks/useProjectDetail.ts

// Ported from upstream Paperclip's pages/ProjectDetail.tsx data-fetching block
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors useGoalDetail.ts: the project row + its project-scoped issues (the
// Issues tab). The Issues tab fetch uses the BFF `?project=` filter (PR #1006).
// Defers upstream's budget-overview / live-runs / workspace queries.

import { useMemo } from "react";
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
type ProjectsResponse = { projects: Project[] } | Project[];

function normalizeIssues(data: IssuesResponse | undefined): Issue[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.issues ?? [];
}

function normalizeProjects(data: ProjectsResponse | undefined): Project[] {
  if (!data) return [];
  return Array.isArray(data) ? data : (data.projects ?? []);
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function useProjectDetail(projectId: string): UseProjectDetailResult {
  const { read } = useTeamsApi();

  // A Paperclip-native link (`/<prefix>/projects/<urlKey>`) may carry the
  // project's upstream url-key rather than its uuid — shellUrlFromPaperclipPath
  // has no project list to resolve it against, and upstream's own
  // `GET /projects/:id` is a strict uuid lookup with no url-key fallback
  // (server/src/services/projects.ts getProjectById). Resolve it against the
  // already-fetched project list before the detail read, but only when it's
  // needed: every id this app itself ever links with IS a uuid, so skip the
  // extra request on that (overwhelmingly common) path.
  const needsResolve = !UUID_RE.test(projectId);
  const projectsList = read<ProjectsResponse>(needsResolve ? teamsQueryKeys.projects() : null);
  const resolvedProjectId = useMemo(() => {
    if (!needsResolve) return projectId;
    const match = normalizeProjects(projectsList.data).find(
      (p) => p.id === projectId || p.urlKey === projectId,
    );
    return match?.id ?? projectId;
  }, [needsResolve, projectId, projectsList.data]);

  // Hold the detail (and issues) fetch until the list has had a chance to
  // resolve a url-key — firing it with the raw key first would 404 needlessly
  // on every load of a url-key link, before falling back to the raw key once
  // the list has settled either way (loaded with no match, or errored).
  const detailReady = !needsResolve || projectsList.data !== undefined || !!projectsList.error;
  const project = read<Project>(detailReady ? teamsQueryKeys.projectDetail(resolvedProjectId) : null);
  // Project-scoped issues for the Issues tab — the BFF forwards `?project=` to
  // upstream's `projectId` filter (PR #1006). Only the project read gates
  // `isError`; an issues-list failure renders an empty Issues tab, not a
  // whole-page error (mirrors useGoalDetail's projects-error handling).
  const issues = read<IssuesResponse>(
    detailReady ? teamsQueryKeys.issuesByProject(resolvedProjectId) : null,
  );

  return {
    project: project.data,
    issues: normalizeIssues(issues.data),
    isLoading: !detailReady || project.isLoading,
    issuesLoading: !detailReady || issues.isLoading,
    isError: !!project.error,
    error: project.error || null,
  };
}
