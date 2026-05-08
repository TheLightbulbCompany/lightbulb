"use client";

import { Receipt } from "lucide-react";
import {
  PanelMasthead,
  PanelEmptyState,
  PanelDataTable,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface CostBreakdown {
  total_cents?: number;
  by_agent?: Array<{ agent_id: string; agent_name?: string; cents: number }>;
  by_project?: Array<{ project_id: string; project_name?: string; cents: number }>;
  period?: string;
}

function fmt(cents: number) {
  return `$${(cents / 100).toFixed(2)}`;
}

type AgentRow = NonNullable<CostBreakdown["by_agent"]>[number];
type ProjectRow = NonNullable<CostBreakdown["by_project"]>[number];

export function CostsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<CostBreakdown>("/costs");

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  const totalCents = data?.total_cents ?? 0;
  const byAgent = data?.by_agent ?? [];
  const byProject = data?.by_project ?? [];
  const hasData =
    !isLoading && (totalCents > 0 || byAgent.length > 0 || byProject.length > 0);

  const agentColumns: PanelDataColumn<AgentRow>[] = [
    {
      key: "agent",
      header: "Agent",
      sortable: true,
      cell: (r) => (
        <div className="font-medium text-[var(--ink)]">
          {r.agent_name ?? r.agent_id}
        </div>
      ),
    },
    {
      key: "cents",
      header: "Cost",
      align: "right",
      cell: (r) => (
        <span className="font-mono text-[var(--ink-soft)]">{fmt(r.cents)}</span>
      ),
    },
  ];

  const projectColumns: PanelDataColumn<ProjectRow>[] = [
    {
      key: "project",
      header: "Project",
      sortable: true,
      cell: (r) => (
        <div className="font-medium text-[var(--ink)]">
          {r.project_name ?? r.project_id}
        </div>
      ),
    },
    {
      key: "cents",
      header: "Cost",
      align: "right",
      cell: (r) => (
        <span className="font-mono text-[var(--ink-soft)]">{fmt(r.cents)}</span>
      ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Spend"
        title="Costs"
        meta={data?.period ? `Period · ${data.period}` : undefined}
      />

      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : !hasData ? (
        <PanelEmptyState
          icon={Receipt}
          headline="No spend recorded."
          supporting="Once your agents start running, this dashboard will summarize spend by agent and by project."
        />
      ) : (
        <>
          <div className="editorial-card p-5">
            <div className="text-xs uppercase tracking-wide text-[var(--mute)]">
              Total
            </div>
            <div className="font-editorial-serif text-4xl mt-1 text-[var(--ink)]">
              {fmt(totalCents)}
            </div>
          </div>

          {byAgent.length > 0 && (
            <section className="flex flex-col gap-3">
              <h2 className="font-editorial-serif text-2xl text-[var(--ink)]">
                By agent
              </h2>
              <PanelDataTable<AgentRow>
                rows={byAgent}
                columns={agentColumns}
                rowKey={(r) => r.agent_id}
              />
            </section>
          )}

          {byProject.length > 0 && (
            <section className="flex flex-col gap-3">
              <h2 className="font-editorial-serif text-2xl text-[var(--ink)]">
                By project
              </h2>
              <PanelDataTable<ProjectRow>
                rows={byProject}
                columns={projectColumns}
                rowKey={(r) => r.project_id}
              />
            </section>
          )}
        </>
      )}
    </div>
  );
}
