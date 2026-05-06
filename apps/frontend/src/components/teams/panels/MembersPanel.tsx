"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Member {
  id: string;
  user_id?: string;
  role?: string;
  email_via_clerk?: string;
  joined_at?: string;
}

export function MembersPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ members: Member[] }>("/members");

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

  const members = data?.members ?? [];

  return (
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-4xl mb-6 text-[var(--ink)]">Members</h1>
      <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
        {members.map((m) => (
          <li
            key={m.id}
            className="p-4 flex justify-between items-center"
          >
            <div>
              <div className="text-sm text-[var(--ink)]">
                {m.email_via_clerk ?? m.user_id ?? m.id}
              </div>
              {m.role && (
                <div className="text-xs uppercase tracking-wide text-[var(--mute)] mt-0.5">
                  {m.role}
                </div>
              )}
            </div>
            {m.joined_at && (
              <div className="text-xs text-[var(--mute)] font-mono">{m.joined_at}</div>
            )}
          </li>
        ))}
        {members.length === 0 && (
          <li className="p-4 font-editorial-serif text-xl italic text-[var(--mute)]">
            No members yet.
          </li>
        )}
      </ul>
    </div>
  );
}
