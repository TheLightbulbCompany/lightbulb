"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Skill {
  id: string;
  name: string;
  description?: string;
  version?: string;
}

export function SkillsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ skills: Skill[] }>("/skills");

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

  const skills = data?.skills ?? [];

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-4xl mb-2 text-[var(--ink)]">Skills</h1>
      <p className="text-xs text-[var(--mute)] mb-6 italic">
        Skills are operator-curated. To add or modify, contact your administrator.
      </p>
      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {skills.map((s) => (
          <li key={s.id} className="p-4">
            <div className="flex justify-between">
              <div className="font-medium text-[var(--ink)]">{s.name}</div>
              {s.version && (
                <div className="text-xs text-[var(--mute)] font-mono">
                  v{s.version}
                </div>
              )}
            </div>
            {s.description && (
              <div className="text-xs text-[var(--ink-soft)] mt-1">{s.description}</div>
            )}
          </li>
        ))}
        {skills.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            No skills available.
          </li>
        )}
      </ul>
    </div>
  );
}
