// Ported from upstream Paperclip's pages/Inbox.tsx data-fetching block
// (paperclip/ui/src/pages/Inbox.tsx) (MIT, (c) 2025 Paperclip AI).
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

export interface UseInboxDataOptions {
  /** When false, the three issue reads are gated off (null SWR key) so the
   *  hook does no fetching until the consumer is ready to show inbox data.
   *  Used by surfaces (e.g. the chat shell's "Get started" guide) that only
   *  need a task-exists signal once the container is live. Default true. */
  enabled?: boolean;
}

type IssueListResponse = { items: Issue[] } | Issue[];

function normalize(data: IssueListResponse | undefined): Issue[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.items ?? [];
}

export function useInboxData(options: UseInboxDataOptions = {}): UseInboxDataResult {
  const { enabled = true } = options;
  const { read } = useTeamsApi();

  // Three reads with stable keys. Per upstream Inbox.tsx the BFF /teams/inbox
  // route accepts ?tab= for filter composition. SWR caches by key, so panels
  // sharing tabs hit the same cache. When `enabled` is false each read passes
  // a null key so SWR makes no request until the consumer is ready.
  const mine = read<IssueListResponse>(enabled ? teamsQueryKeys.inbox.list("mine", {}) : null);
  const recent = read<IssueListResponse>(enabled ? teamsQueryKeys.inbox.list("recent", {}) : null);
  const all = read<IssueListResponse>(enabled ? teamsQueryKeys.inbox.list("all", {}) : null);

  return {
    mineIssues: normalize(mine.data),
    touchedIssues: normalize(recent.data),
    allIssues: normalize(all.data),
    isLoading: mine.isLoading || recent.isLoading || all.isLoading,
    isError: !!(mine.error || recent.error || all.error),
    error: mine.error || recent.error || all.error || null,
  };
}
