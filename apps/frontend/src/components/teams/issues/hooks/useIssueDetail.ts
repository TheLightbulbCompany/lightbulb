// Ported from upstream Paperclip's pages/IssueDetail.tsx data-fetching block
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: just issue + comments. Defers documents, attachments, runs, votes, etc.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Issue, IssueComment } from "@/components/teams/shared/types";

export interface UseIssueDetailResult {
  issue: Issue | undefined;
  comments: IssueComment[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
}

type CommentsResponse = { comments: IssueComment[] } | IssueComment[];

function normalizeComments(data: CommentsResponse | undefined): IssueComment[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  return data.comments ?? [];
}

export function useIssueDetail(issueId: string): UseIssueDetailResult {
  const { read } = useTeamsApi();
  const issue = read<Issue>(teamsQueryKeys.issues.detail(issueId));
  const comments = read<CommentsResponse>(teamsQueryKeys.issues.comments(issueId));

  return {
    issue: issue.data,
    comments: normalizeComments(comments.data),
    isLoading: issue.isLoading || comments.isLoading,
    isError: !!(issue.error || comments.error),
    error: issue.error || comments.error || null,
  };
}
