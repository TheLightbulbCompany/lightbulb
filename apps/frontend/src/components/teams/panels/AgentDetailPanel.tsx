"use client";

import { useState } from "react";
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

export function AgentDetailPanel({ agentId }: { agentId: string }) {
  const { read } = useTeamsApi();
  const { data: agent } = read<Agent>(`/agents/${agentId}`);
  const { data: runs } = read<{ runs: RunRow[] }>(`/agents/${agentId}/runs`);
  const [tab, setTab] = useState<"overview" | "runs" | "config">("overview");

  if (!agent)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );

  const runRows = runs?.runs ?? [];

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-3xl mb-1 text-[var(--ink)]">
        {agent.name}
      </h1>
      <div className="text-sm text-[var(--mute)] mb-6 uppercase tracking-wide">
        {agent.role}
      </div>

      <div className="border-b border-[var(--edge)] mb-6">
        {(["overview", "runs", "config"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm transition-colors ${
              tab === t
                ? "border-b-2 border-[var(--forest)] text-[var(--ink)]"
                : "text-[var(--mute)] hover:text-[var(--ink-soft)]"
            }`}
          >
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <pre className="editorial-card text-xs p-4 whitespace-pre-wrap text-[var(--ink-soft)] bg-[var(--cream-deeper)]">
          {JSON.stringify(agent, null, 2)}
        </pre>
      )}
      {tab === "runs" && (
        <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
          {runRows.map((r) => (
            <li
              key={r.id}
              className="p-3 flex justify-between items-center"
            >
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
