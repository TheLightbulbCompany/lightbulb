// Ported from upstream Paperclip's pages/Inbox.tsx data-fetching block
// (paperclip/ui/src/pages/Inbox.tsx:744-835) (MIT, (c) 2025 Paperclip AI).
// Translated from React Query's useQuery to SWR via our useTeamsApi hook.
// Subset: 3 issue fetches (mine / recent-touched / all). Heartbeat /
// dashboard / live-runs / approvals fetches are not yet ported.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Issue } from "@/components/teams/shared/types";

export interface UseInboxDataResult {
  mineIssues: Issue[];
  touchedIssues: Issue[];
  allIssues: Issue[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

type IssueListResponse = { items: Issue[] } | Issue[];

function normalize(data: IssueListResponse | undefined): Issue[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.items ?? [];
}

export function useInboxData(): UseInboxDataResult {
  const { read } = useTeamsApi();

  // Three reads with stable keys. Per upstream Inbox.tsx the BFF /teams/inbox
  // route accepts ?tab= for filter composition. SWR caches by key, so panels
  // sharing tabs hit the same cache.
  const mine = read<IssueListResponse>(teamsQueryKeys.inbox.list("mine", {}));
  const recent = read<IssueListResponse>(teamsQueryKeys.inbox.list("recent", {}));
  const all = read<IssueListResponse>(teamsQueryKeys.inbox.list("all", {}));

  return {
    mineIssues: normalize(mine.data),
    touchedIssues: normalize(recent.data),
    allIssues: normalize(all.data),
    isLoading: mine.isLoading || recent.isLoading || all.isLoading,
    isError: !!(mine.error || recent.error || all.error),
    error: mine.error || recent.error || all.error || null,
  };
}
