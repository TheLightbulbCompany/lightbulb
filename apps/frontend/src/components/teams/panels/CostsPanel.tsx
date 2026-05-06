"use client";

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

export function CostsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<CostBreakdown>("/costs");

  if (isLoading)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-4xl mb-1 text-[var(--ink)]">Costs</h1>
      {data?.period && (
        <div className="text-xs uppercase tracking-wide text-[var(--mute)] mb-6">
          Period · {data.period}
        </div>
      )}

      <div className="editorial-card p-4 mb-6">
        <div className="text-xs uppercase tracking-wide text-[var(--mute)]">Total</div>
        <div className="font-editorial-serif text-3xl mt-1 text-[var(--ink)]">
          {fmt(data?.total_cents ?? 0)}
        </div>
      </div>

      {data?.by_agent && data.by_agent.length > 0 && (
        <section className="mb-6">
          <h2 className="font-editorial-serif text-2xl mb-2 text-[var(--ink)]">By agent</h2>
          <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
            {data.by_agent.map((row) => (
              <li
                key={row.agent_id}
                className="p-3 flex justify-between text-sm"
              >
                <span className="text-[var(--ink)]">
                  {row.agent_name ?? row.agent_id}
                </span>
                <span className="font-mono text-[var(--ink-soft)]">{fmt(row.cents)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {data?.by_project && data.by_project.length > 0 && (
        <section>
          <h2 className="font-editorial-serif text-2xl mb-2 text-[var(--ink)]">By project</h2>
          <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
            {data.by_project.map((row) => (
              <li
                key={row.project_id}
                className="p-3 flex justify-between text-sm"
              >
                <span className="text-[var(--ink)]">
                  {row.project_name ?? row.project_id}
                </span>
                <span className="font-mono text-[var(--ink-soft)]">{fmt(row.cents)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
