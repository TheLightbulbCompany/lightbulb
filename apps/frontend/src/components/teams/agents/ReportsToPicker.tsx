// apps/frontend/src/components/teams/agents/ReportsToPicker.tsx
"use client";

import type { CompanyAgent } from "@/components/teams/shared/types";

/** Agents eligible to be `agentId`'s manager: everyone except itself and its
 *  descendants (assigning a descendant would create a cycle). */
export function candidateManagers(agents: CompanyAgent[], agentId: string): CompanyAgent[] {
  const descendants = new Set<string>([agentId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const a of agents) {
      if (a.reportsTo && descendants.has(a.reportsTo) && !descendants.has(a.id)) {
        descendants.add(a.id);
        grew = true;
      }
    }
  }
  return agents.filter((a) => !descendants.has(a.id));
}

export function ReportsToPicker({
  agents,
  agentId,
  value,
  onChange,
  disabled,
}: {
  agents: CompanyAgent[];
  agentId: string;
  value: string | null | undefined;
  onChange: (managerId: string | null) => void;
  disabled?: boolean;
}) {
  const candidates = candidateManagers(agents, agentId);
  return (
    <select
      aria-label="Reports to"
      disabled={disabled}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 text-[var(--midnight)]"
    >
      <option value="">No manager (top level)</option>
      {candidates.map((a) => (
        <option key={a.id} value={a.id}>
          {a.name}
        </option>
      ))}
    </select>
  );
}
