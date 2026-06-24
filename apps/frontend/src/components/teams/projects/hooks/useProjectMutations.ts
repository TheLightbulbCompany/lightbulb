// apps/frontend/src/components/teams/projects/hooks/useProjectMutations.ts

// Ported from upstream Paperclip's pages/ProjectDetail.tsx mutations
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors useGoalMutations.ts' update path. (Create lives in the
// ProjectsListPanel's New-project dialog.)
//
// Scope note: no `remove` here — the slim Teams BFF exposes no
// DELETE /projects/{id} route (and the upstream admin client has no
// delete_project), so the Project-detail page omits a delete action rather
// than wiring a 404. Upstream's archive flow (PATCH archivedAt) is likewise
// out of the slim BFF's whitelist and deferred.
//
// Wire shape note: the Teams BFF's PatchProjectBody whitelists snake_case keys
// (name, description) and re-emits camelCase to Paperclip via its alias
// generator — that snake_case IS the wire shape the hook sends.
// ProjectUpdateInput already uses snake_case so it is forwarded verbatim.

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Project, ProjectUpdateInput } from "@/components/teams/shared/types";

const SWR_PREFIX = "/teams";
const swrKey = (path: string) => `${SWR_PREFIX}${path}`;

export interface UseProjectMutationsResult {
  update: (projectId: string, input: ProjectUpdateInput) => Promise<Project>;
}

export function useProjectMutations(): UseProjectMutationsResult {
  const { patch } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const update = useCallback(
    async (projectId: string, input: ProjectUpdateInput): Promise<Project> => {
      const updated = await patch<Project>(`/projects/${projectId}`, input);
      // Invalidate the detail key + the company project list (the list row's
      // name/description reflect the change).
      mutate(swrKey(teamsQueryKeys.projectDetail(projectId)));
      mutate(swrKey(teamsQueryKeys.projects()));
      return updated;
    },
    [patch, mutate],
  );

  return { update };
}
