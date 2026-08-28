"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";
import { DocumentRow } from "./DocumentRow";

function docTime(d: IssueDocument): number {
  const v = d.updatedAt ?? d.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function IssueDocumentsSection({
  issueId,
  agents,
}: {
  issueId: string;
  agents: CompanyAgent[];
}) {
  const { read } = useTeamsApi();
  const { data, error, isLoading } = read<{ documents: IssueDocument[] }>(
    `/issues/${issueId}/documents`,
  );
  const documents = [...(data?.documents ?? [])].sort((a, b) => docTime(b) - docTime(a)); // newest first
  // Only render nothing when there's nothing to show AND nothing to report —
  // a fetch error or in-flight load earns an affordance.
  if (documents.length === 0 && !error && !isLoading) return null;

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label">
        <b>Documents</b>
      </h2>
      {error && (
        <p className="text-xs" style={{ color: "var(--err)" }}>
          Couldn&apos;t load documents.
        </p>
      )}
      {isLoading && documents.length === 0 && !error && (
        <p className="text-xs italic" style={{ color: "var(--midnight-muted)" }}>
          Loading documents…
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {documents.map((d) => (
          <DocumentRow key={d.id} doc={d} agents={agents} issueId={issueId} />
        ))}
      </ul>
    </section>
  );
}
