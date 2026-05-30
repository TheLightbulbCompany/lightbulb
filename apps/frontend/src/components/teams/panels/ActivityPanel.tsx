"use client";

import { useMemo, useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface ActivityEvent {
  id: string;
  type: string;
  actor?: string;
  target?: string;
  createdAt?: string;
  description?: string;
}

export function ActivityPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ events: ActivityEvent[] }>("/activity");
  const [typeFilter, setTypeFilter] = useState("all");

  const events = useMemo(() => data?.events ?? [], [data]);
  const types = useMemo(
    () => Array.from(new Set(events.map((e) => e.type).filter(Boolean))).sort(),
    [events],
  );
  const filtered = typeFilter === "all" ? events : events.filter((e) => e.type === typeFilter);

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
      <div className="flex items-center justify-between mb-6 gap-4">
        <h1 className="font-editorial-serif text-4xl text-[var(--ink)]">Activity</h1>
        {types.length > 0 && (
          <select
            aria-label="Filter by type"
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="border border-[var(--edge)] bg-[var(--cream)] rounded px-2 py-1 text-sm text-[var(--ink)]"
          >
            <option value="all">All types</option>
            {types.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        )}
      </div>
      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {filtered.map((e) => (
          <li key={e.id} className="p-4 flex justify-between items-start gap-4">
            <div>
              <div className="text-xs uppercase tracking-wide text-[var(--mute)]">{e.type}</div>
              <div className="text-sm text-[var(--ink)] mt-0.5">
                {e.description ?? `${e.actor ?? "?"} → ${e.target ?? "?"}`}
              </div>
            </div>
            {e.createdAt && (
              <div className="text-xs text-[var(--mute)] whitespace-nowrap font-mono">{e.createdAt}</div>
            )}
          </li>
        ))}
        {filtered.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            {events.length === 0 ? "No activity yet." : "No activity of this type."}
          </li>
        )}
      </ul>
    </div>
  );
}
