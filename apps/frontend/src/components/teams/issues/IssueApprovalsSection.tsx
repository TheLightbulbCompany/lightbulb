// apps/frontend/src/components/teams/issues/IssueApprovalsSection.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

export function IssueApprovalsSection({ issueId }: { issueId: string }) {
  const { read } = useTeamsApi();
  const { data } = read<{ approvals: Approval[] }>(`/issues/${issueId}/approvals`);
  const approvals = data?.approvals ?? [];
  if (approvals.length === 0) return null;
  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label"><b>Approvals</b></h2>
      <ul className="flex flex-col">
        {approvals.map((a) => (
          <ApprovalRow key={a.id} approval={a} />
        ))}
      </ul>
    </section>
  );
}
