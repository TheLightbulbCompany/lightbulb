"use client";

import { useAuth } from "@clerk/nextjs";
import { CasesList } from "@/components/teams/cases/CasesList";

/** The Cases list (`?panel=teams-cases`). The saved view (filters, columns,
 *  sort) is per workspace, so the list remounts when the workspace changes. */
export function CasesPanel() {
  const { orgId } = useAuth();
  return (
    <div className="mx-auto w-full max-w-[1600px] p-4 text-[var(--fg)] sm:p-5 lg:p-6">
      <CasesList key={orgId ?? ""} storageKey={orgId ? `lightbulb.cases.view:${orgId}` : null} />
    </div>
  );
}
