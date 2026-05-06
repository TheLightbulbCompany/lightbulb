"use client";

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Project {
  id: string;
  name: string;
  description?: string;
  budget_monthly_cents?: number;
}

export function ProjectDetailPanel({ projectId }: { projectId: string }) {
  const { read } = useTeamsApi();
  const { data, isLoading } = read<Project>(`/projects/${projectId}`);
  const [tab, setTab] = useState<"overview" | "issues" | "budget">("overview");

  if (isLoading)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  if (!data)
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Project not found.
      </div>
    );

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-3xl mb-1 text-[var(--ink)]">{data.name}</h1>
      {data.description && (
        <div className="text-sm text-[var(--ink-soft)] mb-6">{data.description}</div>
      )}

      <div className="border-b border-[var(--edge)] mb-6">
        {(["overview", "issues", "budget"] as const).map((t) => (
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
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
      {tab === "issues" && (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Issues per project — coming soon.
        </div>
      )}
      {tab === "budget" && (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Budget breakdown — coming soon.
        </div>
      )}
    </div>
  );
}
