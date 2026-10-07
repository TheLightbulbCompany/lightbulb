// apps/frontend/src/components/teams/issues/hooks/useIssueInteractions.ts

// Mirrors useIssueDetail.ts: SWR read of an issue's agent-initiated thread
// interactions (GET /teams/issues/{id}/interactions). The BFF proxies upstream
// verbatim, so the body is either a bare array or an `{ interactions: [...] }`
// envelope — normalize both.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { IssueThreadInteraction } from "@/components/teams/shared/types";

export interface UseIssueInteractionsResult {
  interactions: IssueThreadInteraction[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

type InteractionsResponse =
  | { interactions: IssueThreadInteraction[] }
  | IssueThreadInteraction[];

function normalizeInteractions(
  data: InteractionsResponse | undefined,
): IssueThreadInteraction[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.interactions ?? [];
}

export function useIssueInteractions(
  issueId: string,
): UseIssueInteractionsResult {
  const { read } = useTeamsApi();
  const interactions = read<InteractionsResponse>(
    teamsQueryKeys.issues.interactions(issueId),
  );

  return {
    interactions: normalizeInteractions(interactions.data),
    isLoading: interactions.isLoading,
    isError: !!interactions.error,
    error: interactions.error || null,
  };
}
