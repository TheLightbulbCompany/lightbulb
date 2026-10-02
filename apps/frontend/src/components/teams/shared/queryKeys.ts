// apps/frontend/src/components/teams/shared/queryKeys.ts

// Ported from upstream Paperclip's queryKeys.ts (paperclip/ui/src/lib/queryKeys.ts)
// (MIT, © 2025 Paperclip AI). Translated from React Query tuple keys to SWR
// string keys.
//
// Path-prefix contract: keys are RELATIVE to the Teams BFF base. They do NOT
// include the leading `/teams` segment because `useTeamsApi.read(path)`
// composes the final SWR cache key + URL by prepending `/teams` itself
// (see apps/frontend/src/hooks/useTeamsApi.ts). Returning `/teams/...`
// here would produce doubled `/teams/teams/...` SWR keys.

export type InboxTab = "mine" | "recent" | "all" | "unread" | "approvals" | "runs" | "joins";

export interface InboxFilters {
  status?: string;
  project?: string;
  assignee?: string;
  creator?: string;
  search?: string;
  limit?: number;
}

function qs(filters: InboxFilters): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(filters)) {
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${k}=${encodeURIComponent(String(v))}`);
  }
  return parts.join("&");
}

export const teamsQueryKeys = {
  inbox: {
    list: (tab: InboxTab, filters: InboxFilters) => {
      const tail = qs(filters);
      return tail ? `/inbox?tab=${tab}&${tail}` : `/inbox?tab=${tab}`;
    },
    approvals: () => `/inbox/approvals`,
    runs: () => `/inbox/runs`,
    liveRuns: () => `/inbox/live-runs`,
  },
  issues: {
    detail: (id: string) => `/issues/${id}`,
    comments: (id: string) => `/issues/${id}/comments`,
    interactions: (id: string) => `/issues/${id}/interactions`,
    feedbackVotes: (id: string) => `/issues/${id}/feedback-votes`,
    externalObjects: (id: string) => `/issues/${id}/external-objects`,
    // POSTed (the ids travel in the body); the path is the cache-key prefix.
    externalObjectSummaries: () => `/issues/external-object-summaries`,
  },
  goals: {
    list: () => `/goals`,
    detail: (id: string) => `/goals/${id}`,
  },
  approvals: { detail: (id: string) => `/approvals/${id}` },
  // The unified "needs you" feed — board approvals AND issue-thread
  // interactions in one server-ranked list. Deliberately parameterless: the
  // count it carries is company-wide (computed upstream before pagination),
  // so every consumer shares one cache entry and one number.
  attention: () => `/attention`,
  runs: { detail: (id: string) => `/runs/${id}` },
  members: () => `/members`,
  // `projects()` returns the company project list. Kept callable for the
  // existing GoalDetail consumer (do NOT convert to a namespace object — it
  // would break `teamsQueryKeys.projects()` call sites).
  projects: () => `/projects`,
  // Project-detail surface. `detail` is the full project row; `issues` is the
  // project-scoped issues list (PR #1006 added the BFF `?project=` filter).
  projectDetail: (id: string) => `/projects/${id}`,
  issuesByProject: (projectId: string) => `/issues?project=${projectId}`,
} as const;
