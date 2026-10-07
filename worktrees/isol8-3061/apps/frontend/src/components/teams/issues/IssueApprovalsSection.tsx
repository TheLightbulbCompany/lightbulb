// apps/frontend/src/components/teams/issues/IssueApprovalsSection.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

export function IssueApprovalsSection({ issueId }: { issueId: string }) {
  const { read } = useTeamsApi();
  const { data, error, isLoading } = read<{ approvals: Approval[] }>(
    `/issues/${issueId}/approvals`,
  );
  const approvals = data?.approvals ?? [];
  // Render nothing only when there's nothing to show and nothing to report —
  // surface a fetch error or in-flight load instead of going silent.
  if (approvals.length === 0 && !error && !isLoading) return null;
  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label"><b>Approvals</b></h2>
      {error && (
        <p className="text-xs" style={{ color: "var(--err)" }}>
          Couldn&apos;t load approvals.
        </p>
      )}
      {isLoading && approvals.length === 0 && !error && (
        <p className="text-xs italic" style={{ color: "var(--midnight-muted)" }}>
          Loading approvals…
        </p>
      )}
      <ul className="flex flex-col">
        {approvals.map((a) => (
          <ApprovalRow key={a.id} approval={a} />
        ))}
      </ul>
    </section>
  );
}
