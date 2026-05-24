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
  },
  approvals: { detail: (id: string) => `/approvals/${id}` },
  runs: { detail: (id: string) => `/runs/${id}` },
  members: () => `/members`,
  projects: () => `/projects`,
} as const;
