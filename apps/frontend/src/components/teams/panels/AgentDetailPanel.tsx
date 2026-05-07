"use client";

import { useState } from "react";
import { PanelMasthead, PanelTabs } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Agent {
  id: string;
  name: string;
  role: string;
}

interface RunRow {
  id: string;
  status: string;
  startedAt?: string;
}

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "runs", label: "Runs" },
  { value: "config", label: "Config" },
];

export function AgentDetailPanel({ agentId }: { agentId: string }) {
  const { read } = useTeamsApi();
  const { data: agent } = read<Agent>(`/agents/${agentId}`);
  const { data: runs } = read<{ runs: RunRow[] }>(`/agents/${agentId}/runs`);
  const [tab, setTab] = useState<string>("overview");

  if (!agent) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  }

  const runRows = runs?.runs ?? [];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Agent"
        title={agent.name}
        meta={agent.role.toUpperCase()}
      />
      <PanelTabs tabs={TABS} value={tab} onValueChange={setTab} />

      {tab === "overview" && (
        <pre className="editorial-card text-xs p-4 whitespace-pre-wrap text-[var(--ink-soft)] bg-[var(--cream-deeper)]">
          {JSON.stringify(agent, null, 2)}
        </pre>
      )}
      {tab === "runs" && (
        <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
          {runRows.map((r) => (
            <li key={r.id} className="p-3 flex justify-between items-center">
              <span className="text-[var(--ink)]">{r.status}</span>
              <a
                href={`/teams/agents/${agentId}/runs/${r.id}`}
                className="text-sm text-[var(--ink-soft)] hover:underline"
              >
                Open →
              </a>
            </li>
          ))}
          {runRows.length === 0 && (
            <li className="p-3 font-editorial-serif text-xl italic text-[var(--mute)]">
              No runs yet.
            </li>
          )}
        </ul>
      )}
      {tab === "config" && (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Adapter configuration is managed by Isol8 and cannot be edited here.
        </div>
      )}
    </div>
  );
}
