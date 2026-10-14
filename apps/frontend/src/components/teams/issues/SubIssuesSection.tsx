// apps/frontend/src/components/teams/issues/SubIssuesSection.tsx
"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { EditorialButton } from "@/components/teams/shell";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { sortSubIssues, buildSubIssueProgressSummary } from "@/components/teams/shared/lib/subIssues";
import type { CompanyAgent, CompanyMember, Issue, IssueProject } from "@/components/teams/shared/types";

export function SubIssuesSection({
  issueId,
  agents,
  members,
  projects,
  parentLabel,
}: {
  issueId: string;
  agents: CompanyAgent[];
  members?: CompanyMember[];
  projects: IssueProject[];
  parentLabel?: string;
}) {
  const { read } = useTeamsApi();
  const { data, error } = read<{ issues: Issue[] }>(`/issues/${issueId}/children`);
  const [dialogOpen, setDialogOpen] = useState(false);

  const children = data?.issues ?? [];
  const sorted = sortSubIssues(children);
  const summary = buildSubIssueProgressSummary(children);
  const showProgress = summary.totalCount >= 2;

  const newButton = (
    <EditorialButton variant="secondary" size="sm" onClick={() => setDialogOpen(true)}>
      <Plus size={16} className="mr-1.5" />
      New Sub-issue
    </EditorialButton>
  );

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      {children.length > 0 ? (
        <>
          <div className="flex items-center justify-between gap-2">
            <h2 className="section-label"><b>Sub-issues</b></h2>
            {newButton}
          </div>
          {showProgress && (
            <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
              {summary.doneCount} / {summary.totalCount} done
              {summary.inProgressCount > 0 ? ` · ${summary.inProgressCount} in progress` : ""}
              {summary.blockedCount > 0 ? ` · ${summary.blockedCount} blocked` : ""}
              {summary.target ? ` · ${summary.target.kind === "next" ? "Next" : "Blocked"}: ${summary.target.issue.title}` : ""}
            </p>
          )}
          <ul className="flex flex-col">
            {sorted.map((c) => (
              <IssueRow key={c.id} issue={c} />
            ))}
          </ul>
        </>
      ) : (
        // Empty state: just the button (no header).
        <div className="flex items-center justify-end gap-2">{newButton}</div>
      )}
      {error && (
        <p className="text-caption" style={{ color: "var(--err)" }}>Couldn&apos;t load sub-issues.</p>
      )}
      <NewIssueDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        agents={agents}
        members={members}
        projects={projects}
        parentId={issueId}
        parentLabel={parentLabel}
        onCreated={() => setDialogOpen(false)}
      />
    </section>
  );
}
