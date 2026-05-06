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

export function useActivityFeed(limit = 10): {
  events: ActivityEvent[];
  isLoading: boolean;
  isError: boolean;
} {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<ActivityResponse>("/activity");
  const events = normalizeEvents(data).slice(0, limit);
  return { events, isLoading, isError: !!error };
}
