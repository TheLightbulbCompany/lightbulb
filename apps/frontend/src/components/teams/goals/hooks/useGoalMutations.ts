// Ported from upstream Paperclip's pages/GoalDetail.tsx mutations
// (paperclip/ui/src/pages/GoalDetail.tsx) (MIT, © 2025 Paperclip AI).
// v1: create + update + remove. Defers asset uploads / sub-goal dialogs.
//
// Wire shape note: the Teams BFF's CreateGoalBody / PatchGoalBody whitelist
// snake_case keys (title, description, parent_id, status) — that snake_case IS
// the wire shape (unlike issues, which forward camelCase). GoalCreateInput /
// GoalUpdateInput already use snake_case so they are forwarded verbatim.

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type {
  Goal,
  GoalCreateInput,
  GoalUpdateInput,
} from "@/components/teams/shared/types";

const SWR_PREFIX = "/teams";
const swrKey = (path: string) => `${SWR_PREFIX}${path}`;

export interface UseGoalMutationsResult {
  create: (input: GoalCreateInput) => Promise<Goal>;
  update: (goalId: string, input: GoalUpdateInput) => Promise<Goal>;
  remove: (goalId: string) => Promise<void>;
}

export function useGoalMutations(): UseGoalMutationsResult {
  const { post, patch, del } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const create = useCallback(
    async (input: GoalCreateInput): Promise<Goal> => {
      const created = await post<Goal>("/goals", input);
      // Revalidate the company goal list so the new goal appears.
      mutate(swrKey(teamsQueryKeys.goals.list()));
      return created;
    },
    [post, mutate],
  );

  const update = useCallback(
    async (goalId: string, input: GoalUpdateInput): Promise<Goal> => {
      const updated = await patch<Goal>(`/goals/${goalId}`, input);
      // Invalidate the detail key + the company goal list (parent/sub-goal
      // rows on the list reflect the change).
      mutate(swrKey(teamsQueryKeys.goals.detail(goalId)));
      mutate(swrKey(teamsQueryKeys.goals.list()));
      return updated;
    },
    [patch, mutate],
  );

  const remove = useCallback(
    async (goalId: string): Promise<void> => {
      await del(`/goals/${goalId}`);
      // Invalidate the company goal list so the deleted goal drops out — the
      // detail key for this id is intentionally not revalidated since the
      // caller navigates away from the now-gone goal.
      mutate(swrKey(teamsQueryKeys.goals.list()));
    },
    [del, mutate],
  );

  return { create, update, remove };
}
