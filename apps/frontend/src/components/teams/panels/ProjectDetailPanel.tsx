"use client";

import { useState } from "react";
import { PanelMasthead, PanelTabs } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Project {
  id: string;
  name: string;
  description?: string;
  budget_monthly_cents?: number;
}

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "issues", label: "Issues" },
  { value: "budget", label: "Budget" },
];

export function ProjectDetailPanel({ projectId }: { projectId: string }) {
  const { read } = useTeamsApi();
  const { data, isLoading } = read<Project>(`/projects/${projectId}`);
  const [tab, setTab] = useState<string>("overview");

  if (isLoading) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  }
  if (!data) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Project not found.
      </div>
    );
  }

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Project" title={data.name} />
      {data.description && (
        <p className="text-sm text-[var(--ink-soft)] -mt-3">
          {data.description}
        </p>
      )}
      <PanelTabs tabs={TABS} value={tab} onValueChange={setTab} />

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
