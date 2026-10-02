// apps/frontend/src/components/teams/cases/hooks/useCases.ts

// Ported from upstream Paperclip's api/cases.ts (paperclip/ui/src/api/cases.ts)
// (MIT, (c) 2025 Paperclip AI). TanStack Query calls became `useTeamsApi()`
// SWR reads against the Teams BFF, which envelopes every list.
//
// Cases emit no live events upstream, so every read here polls: on focus and
// every 30 s (the interval IssuesPanel uses).

import { useTeamsApi, type TeamsApiError } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type {
  CaseDetail,
  CaseEvent,
  CaseStatus,
  CaseSummary,
  IssueCaseLink,
} from "@/components/teams/shared/types";

const POLL = { refreshInterval: 30_000, revalidateOnFocus: true } as const;

export interface ListCasesParams {
  types?: string[];
  status?: string;
  statuses?: string[];
  projectIds?: string[];
  includeNoProject?: boolean;
  labelId?: string;
  q?: string;
  includeAncestors?: boolean;
  limit?: number;
}

export function casesListQuery(params: ListCasesParams): string {
  const search = new URLSearchParams();
  for (const value of params.types ?? []) search.append("types", value);
  if (params.status) search.set("status", params.status);
  for (const value of params.statuses ?? []) search.append("statuses", value);
  for (const value of params.projectIds ?? []) search.append("projectIds", value);
  if (params.includeNoProject) search.set("includeNoProject", "true");
  if (params.labelId) search.set("labelId", params.labelId);
  if (params.q) search.set("q", params.q);
  if (params.includeAncestors) search.set("includeAncestors", "true");
  if (params.limit != null) search.set("limit", String(params.limit));
  return search.toString();
}

/** Upstream answers 403 "Cases are disabled" while the instance flag is off. */
export function isCasesDisabled(error: unknown): boolean {
  return (error as TeamsApiError | undefined)?.status === 403;
}

export function useCasesList(params: ListCasesParams) {
  const { read } = useTeamsApi();
  return read<{ cases: CaseSummary[] }>(teamsQueryKeys.cases.list(casesListQuery(params)), {
    ...POLL,
    keepPreviousData: true,
    // The flag will not flip between retries; the poll picks it up.
    shouldRetryOnError: (err: unknown) => !isCasesDisabled(err),
  });
}

export function useCaseDetail(caseId: string) {
  const { read } = useTeamsApi();
  return read<CaseDetail>(teamsQueryKeys.cases.detail(caseId), POLL);
}

export function useCaseEvents(caseId: string) {
  const { read } = useTeamsApi();
  return read<{ events: CaseEvent[] }>(teamsQueryKeys.cases.events(caseId), POLL);
}

/** Direct children, all statuses: a structural view, not a work queue. */
export function useCaseChildren(caseUuid: string | undefined) {
  const { read } = useTeamsApi();
  return read<{ cases: CaseSummary[] }>(caseUuid ? teamsQueryKeys.cases.children(caseUuid) : null, POLL);
}

export function useIssueCases(issueId: string) {
  const { read } = useTeamsApi();
  // Polled like the other case reads (cases emit no live events). No retry:
  // a 403 means Cases is off, and the next poll picks a flag change up.
  return read<{ cases: IssueCaseLink[] }>(teamsQueryKeys.cases.forIssue(issueId), {
    ...POLL,
    shouldRetryOnError: false,
  });
}

export interface PatchCaseInput {
  status?: CaseStatus;
  labelIds?: string[];
}

export function useCaseMutations() {
  const { patch, mutate } = useTeamsApi();
  return {
    /** PATCH, then refresh every cached cases read (lists, this detail, its events). */
    async patchCase(caseId: string, input: PatchCaseInput): Promise<CaseDetail> {
      const updated = await patch<CaseDetail>(`/cases/${caseId}`, input);
      void mutate((path) => path.startsWith("/teams/cases"));
      return updated;
    },
  };
}
