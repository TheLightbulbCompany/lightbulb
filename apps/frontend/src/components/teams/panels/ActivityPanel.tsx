"use client";

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
  const { data, isLoading, error } = read<{ events: ActivityEvent[] }>(
    "/activity",
  );

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

  const events = data?.events ?? [];

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-4xl mb-6 text-[var(--ink)]">Activity</h1>
      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {events.map((e) => (
          <li key={e.id} className="p-4 flex justify-between items-start gap-4">
            <div>
              <div className="text-xs uppercase tracking-wide text-[var(--mute)]">
                {e.type}
              </div>
              <div className="text-sm text-[var(--ink)] mt-0.5">
                {e.description ?? `${e.actor ?? "?"} → ${e.target ?? "?"}`}
              </div>
            </div>
            {e.createdAt && (
              <div className="text-xs text-[var(--mute)] whitespace-nowrap font-mono">
                {e.createdAt}
              </div>
            )}
          </li>
        ))}
        {events.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            No activity yet.
          </li>
        )}
      </ul>
    </div>
  );
}
