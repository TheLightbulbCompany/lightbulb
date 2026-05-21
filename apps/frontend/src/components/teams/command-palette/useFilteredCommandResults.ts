// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// SWR fan-out for the dynamic search groups (agents/issues/projects).

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Issue, CompanyAgent, IssueProject } from "@/components/teams/shared/types";

export interface FilteredCommandResults {
  agents: CompanyAgent[];
  issues: Issue[];
  projects: IssueProject[];
}

const RESULT_LIMIT = 10;

function normalizeArray<T>(data: unknown, key: string): T[] {
  if (!data) return [];
  if (Array.isArray(data)) return data as T[];
  const obj = data as Record<string, unknown>;
  if (Array.isArray(obj[key])) return obj[key] as T[];
  if (Array.isArray(obj.items)) return obj.items as T[];
  return [];
}

function matches(query: string, ...fields: (string | null | undefined)[]): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return fields.some((f) => f && f.toLowerCase().includes(q));
}

export function useFilteredCommandResults(query: string, enabled: boolean): FilteredCommandResults {
  const { read } = useTeamsApi();
  // Always read; SWR de-dupes by key. Setting enabled=false would require
  // useTeamsApi to support null-key, which we don't need to add for v1.
  const agentsData = read<unknown>("/agents");
  const issuesData = read<unknown>("/issues");
  const projectsData = read<unknown>("/projects");

  return useMemo(() => {
    if (!enabled) return { agents: [], issues: [], projects: [] };
    const agents = normalizeArray<CompanyAgent>(agentsData.data, "agents")
      .filter((a) => matches(query, a.name))
      .slice(0, RESULT_LIMIT);
    const issues = normalizeArray<Issue>(issuesData.data, "issues")
      .filter((i) => matches(query, i.title, i.identifier))
      .slice(0, RESULT_LIMIT);
    const projects = normalizeArray<IssueProject>(projectsData.data, "projects")
      .filter((p) => matches(query, p.name))
      .slice(0, RESULT_LIMIT);
    return { agents, issues, projects };
  }, [agentsData.data, issuesData.data, projectsData.data, query, enabled]);
}
