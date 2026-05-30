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
  const { data } = read<{ documents: IssueDocument[] }>(`/issues/${issueId}/documents`);
  const documents = [...(data?.documents ?? [])].sort((a, b) => docTime(b) - docTime(a)); // newest first
  if (documents.length === 0) return null;

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label">
        <b>Documents</b>
      </h2>
      <ul className="flex flex-col gap-2">
        {documents.map((d) => (
          <DocumentRow key={d.id} doc={d} agents={agents} />
        ))}
      </ul>
    </section>
  );
}
