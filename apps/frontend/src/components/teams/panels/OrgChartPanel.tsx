"use client";

// apps/frontend/src/components/teams/panels/OrgChartPanel.tsx

// Wraps the OrgChart with data fetching + loading/error states. Live status
// updates flow via TeamsEventsProvider's EVENT_KEY_MAP: agent events
// invalidate /teams/agents and the chart re-renders with fresh status colors.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { OrgChart } from "@/components/teams/org-chart/OrgChart";
import type { OrgChartAgent } from "@/components/teams/org-chart/orgChartLayout";

function normalizeAgents(data: unknown): OrgChartAgent[] {
  if (!data) return [];
  if (Array.isArray(data)) return data as OrgChartAgent[];
  const obj = data as Record<string, unknown>;
  if (Array.isArray(obj.agents)) return obj.agents as OrgChartAgent[];
  if (Array.isArray(obj.items)) return obj.items as OrgChartAgent[];
  return [];
}

export function OrgChartPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<unknown>("/agents");

  if (isLoading) {
    return (
      <div className="p-8 text-sm" style={{ color: "var(--mute)" }}>
        Loading...
      </div>
    );
  }
  if (error) {
    return (
      <div
        role="alert"
        className="p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        Failed to load agents.
      </div>
    );
  }

  const agents = normalizeAgents(data);
  return (
    <div className="p-4">
      <h1
        className="font-editorial-serif text-2xl mb-4"
        style={{ color: "var(--ink)" }}
      >
        Org chart
      </h1>
      <OrgChart agents={agents} />
    </div>
  );
}
