// apps/frontend/src/components/teams/dashboard/hooks/useActivityFeed.ts

// Ported from upstream Paperclip's Dashboard activity-feed query
// (paperclip/ui/src/pages/Dashboard.tsx) (MIT, (c) 2025 Paperclip AI).
// SWR-wrapped read of /teams/activity with envelope normalization.

import { useTeamsApi } from "@/hooks/useTeamsApi";

export interface ActivityEvent {
  id: string;
  action: string;
  actorType?: string | null;
  actorId?: string | null;
  /** BFF-enriched (feed.py): the user actor's display name, resolved via the
   *  principalId → Clerk → email join. Falls back to the email. Absent for
   *  agent/system actors and when the join can't resolve the id. */
  actorName?: string | null;
  /** BFF-enriched (feed.py): the user actor's email. */
  actorEmail?: string | null;
  agentId?: string | null;
  entityType?: string | null;
  entityId?: string | null;
  details?: Record<string, unknown> | null;
  createdAt: string;
}

type ActivityResponse =
  | ActivityEvent[]
  | { events: ActivityEvent[] }
  | { items: ActivityEvent[] };

function normalizeEvents(data: ActivityResponse | undefined): ActivityEvent[] {
  if (!data) return [];
  if (Array.isArray(data)) return data;
  if (Array.isArray((data as { events?: unknown }).events)) {
    return (data as { events: ActivityEvent[] }).events;
  }
  if (Array.isArray((data as { items?: unknown }).items)) {
    return (data as { items: ActivityEvent[] }).items;
  }
  return [];
}

// How many events to fetch from the server. The BFF forwards this to
// upstream (bounded to its MAX_ACTIVITY_LIMIT of 500); without it the feed
// silently caps at upstream's default of 100. 200 matches the Activity
// panel's full-list need while staying well under the ceiling.
const ACTIVITY_FETCH_LIMIT = 200;

export function useActivityFeed(
  limit = 10,
  fetchLimit = ACTIVITY_FETCH_LIMIT,
): {
  events: ActivityEvent[];
  isLoading: boolean;
  isError: boolean;
} {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<ActivityResponse>(
    `/activity?limit=${fetchLimit}`,
  );
  // `limit` slices the *displayed* set; the server fetch already pulled up to
  // `fetchLimit` rows.
  const events = normalizeEvents(data).slice(0, limit);
  return { events, isLoading, isError: !!error };
}
