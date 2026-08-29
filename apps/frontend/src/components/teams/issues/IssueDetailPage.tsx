// apps/frontend/src/components/teams/issues/IssueDetailPage.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx top-level layout
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: header + description + comments thread + read-only properties sidebar.
// Drops chat/activity/related-work tabs, run ledger, sub-issues, plugin slots,
// documents, feedback, file uploads, interactions, continuation handoff.

"use client";

import { useState, useEffect, useRef } from "react";
import { useUser } from "@clerk/nextjs";
import { Copy, EyeOff, MoreHorizontal, Trash2 } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { IssueHeader } from "./IssueHeader";
import { IssueComments } from "./IssueComments";
import { IssueProperties } from "./IssueProperties";
import { SubIssuesSection } from "./SubIssuesSection";
import { IssueApprovalsSection } from "./IssueApprovalsSection";
import { IssueRunsSection } from "./IssueRunsSection";
import { IssueTreeControlSection } from "./IssueTreeControlSection";
import { IssueDocumentsSection } from "./IssueDocumentsSection";
import { IssueWorkProductsSection } from "./IssueWorkProductsSection";
import { IssueAttachmentsSection } from "./IssueAttachmentsSection";
import { hideIssueWithUndo } from "./lib/hideIssue";
import { IssueParentBreadcrumb } from "./IssueParentBreadcrumb";
import { useIssueDetail } from "./hooks/useIssueDetail";
import { useIssueMutations } from "./hooks/useIssueMutations";
import { useIssueInteractions } from "./hooks/useIssueInteractions";
import { useInteractionMutations } from "./hooks/useInteractionMutations";
import { useIssueFeedback } from "./hooks/useIssueFeedback";
import { notifyApiError } from "@/lib/notify";
import { patchExecutionPolicy } from "./executionPolicy";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { replaceSearchParams } from "@/lib/shellNav";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import type {
  CompanyAgent,
  CompanyMember,
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
          background: "var(--cream-deep)",
          border: "1px solid var(--edge)",
          color: "var(--midnight)",
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
          color: "var(--midnight-soft)",
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
        color: "var(--midnight-muted)",
      }}
    >
      Add a description…
    </button>
  );
}

export function IssueDetailPage({ issueId }: IssueDetailPageProps) {
  const { issue, comments, isLoading, isError, error } = useIssueDetail(issueId);
  const { interactions } = useIssueInteractions(issueId);
  const mutations = useIssueMutations();
  const interactionMutations = useInteractionMutations();
  const { votesByTarget, castVote } = useIssueFeedback(issueId);
  const { user } = useUser();
  const { read } = useTeamsApi();
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsResp } = read<{ projects: IssueProject[] }>("/projects");
  // Company members power the human-user side of the assignee picker. Mirrors
  // how agents/projects are read; the BFF enriches each row with Clerk identity.
  const { data: membersResp } = read<{ members: CompanyMember[] }>("/members");
  const agents = agentsResp?.agents ?? [];
  const projects = projectsResp?.projects ?? [];
  const members = membersResp?.members ?? [];

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // The issue is gone (deleted or hidden) — swap the now-dead detail view for
  // the Tasks list. In-shell this goes through the nav context (which commits a
  // replace-only URL move so Back never resurrects the dead row); standalone /
  // test renders (no provider) fall back to the direct window.location rewrite.
  const nav = useInlineTeamsNav();
  const leaveForTasksList = () => {
    if (nav) {
      // Pass our own id so the desktop dock closes THIS detail panel, not
      // whichever panel happens to be active when the delete confirms.
      nav.leaveToList("teams-issues", issueId);
      return;
    }
    const params = new URLSearchParams(window.location.search);
    params.set("panel", "teams-issues");
    params.delete("detailId");
    replaceSearchParams(params);
  };

  const confirmDelete = async () => {
    setDeleting(true);
    try {
      await mutations.remove(issueId);
      setDeleteOpen(false);
      leaveForTasksList();
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
    // Hiding stamps hiddenAt, so it skips the confirm dialog that guards the
    // destructive delete — but nothing in the product can list a hidden task
    // afterwards, so the Undo toast is the only way back (see lib/hideIssue).
    // The hidden task drops out of the Tasks list, so leave the detail view.
    await hideIssueWithUndo(
      issueId,
      (id, patch) => mutations.update(id, patch),
      leaveForTasksList,
    );
  };

  if (isLoading && !issue) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
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
        style={{ color: "var(--err)" }}
      >
        <p>Failed to load issue.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--midnight-muted)" }}>
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
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
        }}
      >
        Issue not found.
      </div>
    );
  }

  return (
    <div
      className="grid grid-cols-1 @3xl:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--midnight)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        {/* Hidden tasks are unreachable from every list, board, and search —
            upstream applies `hidden_at IS NULL` to all of them and offers no
            include-hidden filter. Fetch-by-id does NOT filter, though, so this
            page still opens for anyone holding the link. That makes this banner
            the only durable restore path once the Undo toast is gone; without
            it the page renders as a normal task and silently stays invisible. */}
        {issue.hiddenAt && (
          <div
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-sm px-3 py-2 text-sm"
            style={{
              border: "1px solid var(--edge)",
              background: "var(--cream-deep)",
              color: "var(--midnight-soft)",
            }}
          >
            <EyeOff size={16}
              className="shrink-0"
              style={{ color: "var(--midnight-muted)" }}
              aria-hidden="true"
            />
            <span>This task is hidden — it won&apos;t show in Tasks or search.</span>
            <button
              type="button"
              onClick={() => {
                void mutations
                  .update(issueId, { hiddenAt: null })
                  .catch((e) =>
                    notifyApiError(e, "Couldn't restore the task."),
                  );
              }}
              className="hover:underline"
              style={{ color: "var(--midnight)" }}
            >
              Restore
            </button>
          </div>
        )}
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
                className="mt-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-[var(--cream-deep)]"
                style={{ color: "var(--midnight-muted)" }}
                aria-label="Issue actions"
              >
                <MoreHorizontal size={16} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  copyAsMarkdown();
                }}
              >
                <Copy size={16} />
                Copy as markdown
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={(e) => {
                  e.preventDefault();
                  void handleHide();
                }}
              >
                <EyeOff size={16} />
                Hide task
              </DropdownMenuItem>
              <DropdownMenuItem
                variant="destructive"
                onSelect={(e) => {
                  e.preventDefault();
                  setDeleteOpen(true);
                }}
              >
                <Trash2 size={16} />
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
        <IssueTreeControlSection issueId={issueId} />
        <IssueDocumentsSection issueId={issueId} agents={agents} />
        <IssueWorkProductsSection issueId={issueId} />
        <IssueAttachmentsSection issueId={issueId} />
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
            interactions={interactions}
            currentUserId={user?.id ?? null}
            onAcceptInteraction={async (interaction, selectedClientKeys, selectedOptionIds) => {
              const body: Record<string, unknown> = {};
              if (selectedClientKeys) body.selectedClientKeys = selectedClientKeys;
              if (selectedOptionIds) body.selectedOptionIds = selectedOptionIds;
              await interactionMutations.accept(issueId, interaction.id, body);
            }}
            onRejectInteraction={async (interaction, reason) => {
              await interactionMutations.reject(issueId, interaction.id, reason);
            }}
            onSubmitInteractionAnswers={async (interaction, answers) => {
              await interactionMutations.respond(issueId, interaction.id, { answers });
            }}
            onCancelInteraction={async (interaction) => {
              await interactionMutations.cancel(issueId, interaction.id);
            }}
            feedbackVotes={votesByTarget}
            onVoteComment={async (commentId, vote, options) => {
              try {
                await castVote(commentId, vote, options);
              } catch (e) {
                notifyApiError(e, "Failed to save feedback");
                // Re-throw so OutputFeedbackButtons can revert its optimistic
                // vote state — swallowing here leaves the button stuck showing a
                // vote that never persisted.
                throw e;
              }
            }}
          />
        </div>
      </main>
      <aside className="@3xl:sticky @3xl:top-4 @3xl:self-start">
        <IssueProperties
          issue={issue}
          agents={agents}
          members={members}
          projects={projects}
          onStatusChange={async (status) => {
            await mutations.update(issueId, { status });
          }}
          onPriorityChange={async (priority) => {
            await mutations.update(issueId, { priority });
          }}
          onAssigneeChange={(assigneeAgentId) => {
            // Agent and human assignees are mutually exclusive — picking an
            // agent clears any human assignee in the same PATCH.
            void mutations.update(issueId, {
              assigneeAgentId,
              assigneeUserId: null,
            });
          }}
          onAssigneeUserChange={(assigneeUserId) => {
            // Picking a human clears any agent assignee in the same PATCH.
            void mutations.update(issueId, {
              assigneeUserId,
              assigneeAgentId: null,
            });
          }}
          onProjectChange={(projectId) => {
            void mutations.update(issueId, { projectId });
          }}
          onGoalChange={(goalId) => {
            void mutations.update(issueId, { goalId });
          }}
          onBlockedByChange={(blockedByIssueIds) => {
            void mutations.update(issueId, { blockedByIssueIds });
          }}
          onLabelsChange={(labelIds) => {
            void mutations.update(issueId, { labelIds });
          }}
          onParentChange={(parentId) => {
            void mutations.update(issueId, { parentId });
          }}
          onMonitorChange={(monitor) => {
            // executionPolicy bundles monitor + reviewer/approver stages under
            // REPLACE-on-the-wire semantics, so editing the monitor must rebuild
            // the WHOLE policy from the issue's current one — preserving any
            // stages — rather than sending a bare { monitor }. patchExecutionPolicy
            // is the single read-modify-write that guarantees this.
            void mutations.update(issueId, {
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                monitor,
              }),
            });
          }}
          onMonitorClear={() => {
            // Clearing the monitor preserves any reviewer/approver stages; the
            // helper returns null only when nothing else remains.
            void mutations.update(issueId, {
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                monitor: null,
              }),
            });
          }}
          onReviewersChange={(reviewers) => {
            void mutations.update(issueId, {
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                reviewers,
              }),
            });
          }}
          onApproversChange={(approvers) => {
            void mutations.update(issueId, {
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                approvers,
              }),
            });
          }}
          onCheckNow={(id) => mutations.monitorCheckNow(id)}
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
        <p className="text-sm" style={{ color: "var(--midnight-soft)" }}>
          {issue.identifier ? `${issue.identifier}: ` : ""}
          {issue.title}
        </p>
      </EditorialDialog>
    </div>
  );
}
