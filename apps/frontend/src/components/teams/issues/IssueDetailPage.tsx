// apps/frontend/src/components/teams/issues/IssueDetailPage.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx top-level layout
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: header + description + comments thread + read-only properties sidebar.
// Drops chat/activity/related-work tabs, run ledger, sub-issues, plugin slots,
// documents, feedback, file uploads, interactions, continuation handoff.

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { MoreHorizontal, Trash2 } from "lucide-react";
import { IssueHeader } from "./IssueHeader";
import { IssueComments } from "./IssueComments";
import { IssueProperties } from "./IssueProperties";
import { SubIssuesSection } from "./SubIssuesSection";
import { IssueApprovalsSection } from "./IssueApprovalsSection";
import { IssueRunsSection } from "./IssueRunsSection";
import { IssueDocumentsSection } from "./IssueDocumentsSection";
import { IssueParentBreadcrumb } from "./IssueParentBreadcrumb";
import { useIssueDetail } from "./hooks/useIssueDetail";
import { useIssueMutations } from "./hooks/useIssueMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader as DialogHeaderUi,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type {
  CompanyAgent,
  IssueProject,
} from "@/components/teams/shared/types";

export interface IssueDetailPageProps {
  issueId: string;
}

export function IssueDetailPage({ issueId }: IssueDetailPageProps) {
  const { issue, comments, isLoading, isError, error } = useIssueDetail(issueId);
  const mutations = useIssueMutations();
  const router = useRouter();
  const { read } = useTeamsApi();
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsResp } = read<{ projects: IssueProject[] }>("/projects");
  const agents = agentsResp?.agents ?? [];
  const projects = projectsResp?.projects ?? [];

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const confirmDelete = async () => {
    setDeleting(true);
    try {
      await mutations.remove(issueId);
      setDeleteOpen(false);
      // The issue is gone — leave the now-dead detail route for the inbox.
      router.push("/teams");
    } finally {
      setDeleting(false);
    }
  };

  if (isLoading && !issue) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Loading…
      </div>
    );
  }

  if (isError && !issue) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load issue.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--mute)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!issue) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Issue not found.
      </div>
    );
  }

  return (
    <div
      className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        {issue.parentId && <IssueParentBreadcrumb parentId={issue.parentId} />}
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <IssueHeader
              issue={issue}
              agents={agents}
              onTitleSave={async (title) => {
                await mutations.update(issueId, { title });
              }}
              onStatusChange={async (status) => {
                await mutations.update(issueId, { status });
              }}
              onPriorityChange={async (priority) => {
                await mutations.update(issueId, { priority });
              }}
              onAssigneeChange={async (assigneeAgentId) => {
                await mutations.update(issueId, { assigneeAgentId });
              }}
            />
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="mt-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded transition-colors hover:bg-[var(--cream-deeper)]"
                style={{ color: "var(--mute)" }}
                aria-label="Issue actions"
              >
                <MoreHorizontal className="h-4 w-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                variant="destructive"
                onSelect={(e) => {
                  e.preventDefault();
                  setDeleteOpen(true);
                }}
              >
                <Trash2 className="h-4 w-4" />
                Delete issue
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {issue.description && (
          <div
            className="text-sm whitespace-pre-wrap pl-3"
            style={{
              borderLeft: "2px solid var(--edge)",
              color: "var(--ink-soft)",
            }}
          >
            {issue.description}
          </div>
        )}
        <SubIssuesSection
          issueId={issueId}
          agents={agents}
          projects={projects}
          parentLabel={issue.identifier ? `${issue.identifier} ${issue.title}` : issue.title}
        />
        <IssueApprovalsSection issueId={issueId} />
        <IssueRunsSection issueId={issueId} agents={agents} />
        <IssueDocumentsSection issueId={issueId} agents={agents} />
        <div className="pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
          <h2 className="section-label mb-3">
            <b>Conversation</b>
          </h2>
          <IssueComments
            comments={comments}
            agents={agents}
            isLoading={isLoading}
            onSubmit={async (body) => {
              await mutations.addComment(issueId, body);
            }}
          />
        </div>
      </main>
      <aside className="lg:sticky lg:top-4 lg:self-start">
        <IssueProperties
          issue={issue}
          agents={agents}
          projects={projects}
          onAssigneeChange={(assigneeAgentId) => {
            void mutations.update(issueId, { assigneeAgentId });
          }}
          onProjectChange={(projectId) => {
            void mutations.update(issueId, { projectId });
          }}
          onBlockedByChange={(blockedByIssueIds) => {
            void mutations.update(issueId, { blockedByIssueIds });
          }}
        />
      </aside>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeaderUi>
            <DialogTitle>Delete issue?</DialogTitle>
            <DialogDescription>
              This permanently deletes {issue.identifier ? `${issue.identifier}: ` : ""}
              {issue.title}. This action cannot be undone.
            </DialogDescription>
          </DialogHeaderUi>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={deleting}>
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="button"
              variant="destructive"
              disabled={deleting}
              onClick={() => void confirmDelete()}
            >
              {deleting ? "Deleting…" : "Delete issue"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
