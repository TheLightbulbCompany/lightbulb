// apps/frontend/src/components/teams/issues/IssueDetailPage.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx top-level layout
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: header + description + comments thread + read-only properties sidebar.
// Drops chat/activity/related-work tabs, run ledger, sub-issues, plugin slots,
// documents, feedback, file uploads, interactions, continuation handoff.

"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { Copy, EyeOff, MoreHorizontal, Trash2 } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
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
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import type {
  CompanyAgent,
  IssueProject,
} from "@/components/teams/shared/types";

export interface IssueDetailPageProps {
  issueId: string;
}

/** Inline-editable description, mirroring IssueHeader's title-edit pattern so
 * both editing surfaces feel the same. Empty descriptions show a muted
 * "Add a description…" affordance so the field is discoverable. Saving goes
 * through the same PATCH the rest of the detail page uses. */
function IssueDescription({
  description,
  onSave,
}: {
  description: string;
  onSave: (description: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(description);
  const [saving, setSaving] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Re-sync when the description changes externally (mutation resolves and the
  // parent re-renders) — only while not actively editing, so a stale prop
  // never clobbers an in-progress edit.
  useEffect(() => {
    if (!editing) setDraft(description);
  }, [description, editing]);

  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
      // Place the caret at the end rather than selecting the whole body.
      const len = textareaRef.current.value.length;
      textareaRef.current.setSelectionRange(len, len);
    }
  }, [editing]);

  const save = async () => {
    const trimmed = draft.trim();
    if (trimmed === description.trim()) {
      setDraft(description);
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(trimmed);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(description);
    setEditing(false);
  };

  if (editing) {
    return (
      <Textarea
        ref={textareaRef}
        rows={4}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void save();
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancel();
          }
        }}
        disabled={saving}
        placeholder="Add a description…"
        aria-label="Issue description"
        style={{
          background: "var(--cream-deeper)",
          border: "1px solid var(--edge)",
          color: "var(--ink)",
        }}
      />
    );
  }

  if (description) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        className="w-full text-left text-sm whitespace-pre-wrap pl-3"
        style={{
          borderLeft: "2px solid var(--edge)",
          color: "var(--ink-soft)",
        }}
      >
        {description}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="w-full text-left text-sm italic pl-3"
      style={{
        borderLeft: "2px solid var(--edge)",
        color: "var(--mute)",
      }}
    >
      Add a description…
    </button>
  );
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

  const copyAsMarkdown = () => {
    if (!issue) return;
    const heading = issue.identifier ?? issue.id;
    const markdown = `# ${heading}: ${issue.title}\n\n${issue.description ?? ""}`;
    void navigator.clipboard.writeText(markdown);
  };

  const handleHide = async () => {
    // Hiding is reversible (it just stamps hiddenAt), so it skips the confirm
    // dialog that guards the destructive delete. The hidden task drops out of
    // the Tasks list, so leave the detail route for the inbox afterwards.
    await mutations.update(issueId, { hiddenAt: new Date().toISOString() });
    router.push("/teams");
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
              onTitleSave={async (title) => {
                await mutations.update(issueId, { title });
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
                onSelect={(e) => {
                  e.preventDefault();
                  copyAsMarkdown();
                }}
              >
                <Copy className="h-4 w-4" />
                Copy as markdown
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  void handleHide();
                }}
              >
                <EyeOff className="h-4 w-4" />
                Hide task
              </DropdownMenuItem>
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
        <IssueDescription
          description={issue.description ?? ""}
          onSave={async (description) => {
            await mutations.update(issueId, { description });
          }}
        />
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
          onStatusChange={async (status) => {
            await mutations.update(issueId, { status });
          }}
          onPriorityChange={async (priority) => {
            await mutations.update(issueId, { priority });
          }}
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

      <EditorialDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete issue?"
        description={
          <>
            This permanently deletes{" "}
            {issue.identifier ? `${issue.identifier}: ` : ""}
            {issue.title}. This action cannot be undone.
          </>
        }
        footer={
          <>
            <EditorialButton
              variant="secondary"
              size="sm"
              onClick={() => setDeleteOpen(false)}
              disabled={deleting}
            >
              Cancel
            </EditorialButton>
            <EditorialButton
              variant="destructive"
              size="sm"
              disabled={deleting}
              onClick={() => void confirmDelete()}
            >
              {deleting ? "Deleting…" : "Delete issue"}
            </EditorialButton>
          </>
        }
      >
        <p className="text-sm" style={{ color: "var(--ink-soft)" }}>
          {issue.identifier ? `${issue.identifier}: ` : ""}
          {issue.title}
        </p>
      </EditorialDialog>
    </div>
  );
}
