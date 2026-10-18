// apps/frontend/src/components/teams/issues/IssueDetailPage.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx top-level layout
// (ui/src/pages/IssueDetail.tsx @ c6f5a4d2 on isol8/2026.916-rebase)
// (MIT, (c) 2025 Paperclip AI), in stock's classic issue layout (owner ruling
// 2026-09-27: the task page follows stock Paperclip, not the chat shell): the
// breadcrumb, identifier + title and actions; the status cards (monitor banner,
// recovery card, execution blocker); the description as the issue's body
// (IssueDescription); then stock's CommentThread — one chronological timeline
// of comments, status/assignee changes and runs, the live run row with Stop,
// and the "Leave a comment..." composer; the continuation handoff and
// scheduled-retry card below the thread, where stock puts the handoff. The
// properties pane, sub-tasks, approvals, run ledger, documents, output and
// attachments sit in the side column. Drops the related-work tab and plugin
// slots.

"use client";

import { AttachmentIssueContext } from "@/components/teams/shared/lib/attachments";
import { useState } from "react";
import { useUser } from "@clerk/nextjs";
import { Copy, EyeOff, MoreHorizontal, Trash2 } from "lucide-react";
import { IssueHeader } from "./IssueHeader";
import { CommentThread } from "./CommentThread";
import { IssueDescription } from "./IssueDescription";
import { IssueProperties } from "./IssueProperties";
import { IssueCasesPanel } from "@/components/teams/cases/IssueCasesPanel";
import { SubIssuesSection } from "./SubIssuesSection";
import { IssueApprovalsSection } from "./IssueApprovalsSection";
import { IssueRunLedger } from "./IssueRunLedger";
import { ExecutionBlockerNotice } from "./ExecutionBlockerNotice";
import { IssueBlockedNotice } from "./IssueBlockedNotice";
import { collectLiveIssueIds } from "./lib/liveIssueIds";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { IssueTreeControlSection } from "./IssueTreeControlSection";
import { IssueDocumentsSection } from "./IssueDocumentsSection";
import { IssueWorkProductsSection } from "./IssueWorkProductsSection";
import { IssueAttachmentsSection } from "./IssueAttachmentsSection";
import { hideIssueWithUndo } from "./lib/hideIssue";
import { IssueParentBreadcrumb } from "./IssueParentBreadcrumb";
import {
  IssueMonitorBanner,
  IssueMonitorComposerStrip,
  hasVisibleMonitorSurface,
} from "./IssueMonitorBanner";
import { IssueScheduledRetryCard } from "./IssueScheduledRetryCard";
import {
  IssueContinuationHandoff,
  ISSUE_CONTINUATION_SUMMARY_DOCUMENT_KEY,
} from "./IssueContinuationHandoff";
import {
  IssueRecoveryActionCard,
  type RecoveryResolveOutcome,
} from "./IssueRecoveryActionCard";
import { useIssueDetail } from "./hooks/useIssueDetail";
import { useIssueMutations } from "./hooks/useIssueMutations";
import { useIssueInteractions } from "./hooks/useIssueInteractions";
import { useInteractionMutations } from "./hooks/useInteractionMutations";
import { useIssueFeedback } from "./hooks/useIssueFeedback";
import { notifyApiError, notifySuccess } from "@/lib/notify";
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
  IssueDocument,
  IssueProject,
  IssueRecoveryActionsResponse,
} from "@/components/teams/shared/types";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";

// Upstream IssueDetail.handleResolveRecoveryAction: each card menu outcome is one
// (outcome, sourceIssueStatus) pair on POST /issues/:id/recovery-actions/resolve.
const RECOVERY_RESOLVE_REQUEST: Record<
  RecoveryResolveOutcome,
  { outcome: "restored" | "false_positive"; sourceIssueStatus: "todo" | "done" | "in_review" }
> = {
  todo: { outcome: "restored", sourceIssueStatus: "todo" },
  done: { outcome: "restored", sourceIssueStatus: "done" },
  in_review: { outcome: "restored", sourceIssueStatus: "in_review" },
  false_positive_done: { outcome: "false_positive", sourceIssueStatus: "done" },
  false_positive_in_review: { outcome: "false_positive", sourceIssueStatus: "in_review" },
};

export interface IssueDetailPageProps {
  issueId: string;
}

// A task can be opened by identifier (INT-7, e.g. from a Case link) or by uuid.
// Paperclip's detail route accepts either, but its sub-resource routes
// (children, ...) need the uuid, and every cache key and live-event match is
// uuid-keyed. So resolve the issue first and run the whole page against
// `issue.id`; `routeId` is only the panel's own id (for closing the dock).
export function IssueDetailPage({ issueId }: IssueDetailPageProps) {
  const { issue, isLoading, isError, error } = useIssueDetail(issueId);
  if (!issue) return <IssueDetailStatus isLoading={isLoading} error={isError ? error : null} />;
  return <IssueDetailView key={issue.id} issueId={issue.id} routeId={issueId} />;
}

function IssueDetailStatus({ isLoading, error }: { isLoading: boolean; error: Error | null }) {
  if (isLoading) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-ui italic"
        style={{
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
        }}
      >
        Loading…
      </div>
    );
  }

  if (error) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-ui"
        style={{ color: "var(--err)" }}
      >
        <p>Failed to load issue.</p>
        {error.message && (
          <p className="mt-1" style={{ color: "var(--midnight-muted)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  return (
    <div
      className="flex h-full items-center justify-center p-8 text-ui italic"
      style={{
        fontFamily: "var(--font-serif), serif",
        color: "var(--midnight-muted)",
      }}
    >
      Issue not found.
    </div>
  );
}

function IssueDetailView({ issueId, routeId }: { issueId: string; routeId: string }) {
  const { issue, comments, isLoading, isError, error } = useIssueDetail(issueId);
  const { interactions } = useIssueInteractions(issueId);
  const mutations = useIssueMutations();
  // Property edits are fire-and-forget; a rejection (e.g. Paperclip's "Issue is blocked by
  // unresolved blockers") must still reach the user rather than vanish.
  const updateIssue = (input: Parameters<typeof mutations.update>[1]) =>
    void mutations.update(issueId, input).catch((e) => notifyApiError(e, "Couldn't update the task."));
  const interactionMutations = useInteractionMutations();
  const { votesByTarget, castVote } = useIssueFeedback(issueId);
  const { user } = useUser();
  const { read, post, put, del, mutate } = useTeamsApi();
  const recovery = read<IssueRecoveryActionsResponse>(`/issues/${issueId}/recovery-actions`);
  const recoveryAction = recovery.data?.active ?? null;
  const { runs: liveRuns } = useLiveRuns();
  // Upstream reads the continuation handoff as its own system document (the
  // documents list hides system keys); a 404 just means there is no handoff.
  const { data: continuationHandoff } = read<IssueDocument>(
    `/issues/${issueId}/documents/${ISSUE_CONTINUATION_SUMMARY_DOCUMENT_KEY}`,
    { shouldRetryOnError: false },
  );
  const [checkingMonitorNow, setCheckingMonitorNow] = useState(false);
  const checkMonitorNow = () => {
    setCheckingMonitorNow(true);
    void mutations
      .monitorCheckNow(issueId)
      .then(() => {
        notifySuccess("Monitor check queued");
        void mutate(`/teams${teamsQueryKeys.issues.detail(issueId)}`);
      })
      .catch((e) => notifyApiError(e, "Monitor check failed"))
      .finally(() => setCheckingMonitorNow(false));
  };
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsResp } = read<{ projects: IssueProject[] }>("/projects");
  // Company members power the human-user side of the assignee picker. Mirrors
  // how agents/projects are read; the BFF enriches each row with Clerk identity.
  const { data: membersResp } = read<{ members: CompanyMember[] }>("/members");
  const agents = agentsResp?.agents ?? [];
  const projects = projectsResp?.projects ?? [];
  const members = membersResp?.members ?? [];

  const agentMap = new Map(agents.map((a) => [a.id, a]));

  const resolveRecoveryAction = (outcome: RecoveryResolveOutcome) => {
    if (!recoveryAction) return;
    void post(`/issues/${issueId}/recovery-actions/resolve`, {
      actionId: recoveryAction.id,
      ...RECOVERY_RESOLVE_REQUEST[outcome],
    })
      .catch((e) => notifyApiError(e, "Unable to resolve recovery action"))
      .finally(() => {
        void recovery.mutate();
        void mutate(`/teams${teamsQueryKeys.issues.detail(issueId)}`);
      });
  };

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
      nav.leaveToList("teams-issues", routeId);
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
    } catch (err) {
      notifyApiError(err, "Couldn't delete the task");
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

  if (!issue) return <IssueDetailStatus isLoading={isLoading} error={isError ? error : null} />;

  return (
    // Every agent-pasted attachment link / image on this page (description,
    // comments, interactions, documents, outputs) fetches issue-scoped bytes.
    <AttachmentIssueContext.Provider value={issue.id}>
    <div
      className="grid grid-cols-1 @3xl:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--midnight)" }}
    >
      <main className="flex min-w-0 flex-col gap-6">
        <div className="flex flex-col gap-4">
          {/* Hidden tasks are unreachable from every list, board, and search —
              upstream applies `hidden_at IS NULL` to all of them and offers no
              include-hidden filter. Fetch-by-id does NOT filter, though, so this
              page still opens for anyone holding the link. That makes this banner
              the only durable restore path once the Undo toast is gone; without
              it the page renders as a normal task and silently stays invisible. */}
          {issue.hiddenAt && (
            <div
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-sm px-3 py-2 text-ui"
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
          <IssueMonitorBanner
            issue={issue}
            onCheckNow={checkMonitorNow}
            checkingNow={checkingMonitorNow}
          />
          {recoveryAction ? (
            <IssueRecoveryActionCard
              className="mb-4"
              action={recoveryAction}
              agentMap={agentMap}
              scheduledRetry={issue.scheduledRetry ?? null}
              onResolve={resolveRecoveryAction}
              canFalsePositive
            />
          ) : null}
          {issue.executionBlocker ? (
            <ExecutionBlockerNotice issueId={issue.id} blocker={issue.executionBlocker} />
          ) : null}
          {/* Upstream IssueChatThread's mount: unresolved = blockedBy minus done/cancelled,
              and the handoff alarm yields to an active recovery action. */}
          <IssueBlockedNotice
            issueId={issue.id}
            issueStatus={issue.status}
            blockers={(issue.blockedBy ?? []).filter(
              (b) => b.status !== "done" && b.status !== "cancelled",
            )}
            allBlockers={issue.blockedBy ?? []}
            liveIssueIds={collectLiveIssueIds(liveRuns, [issue])}
            blockerAttention={typeof issue.blockerAttention === "object" ? issue.blockerAttention : null}
            successfulRunHandoff={
              (recoveryAction ?? issue.activeRecoveryAction) ? null : (issue.successfulRunHandoff ?? null)
            }
            scheduledRetry={issue.scheduledRetry ?? null}
            agentName={
              issue.successfulRunHandoff?.assigneeAgentId
                ? (agentMap.get(issue.successfulRunHandoff.assigneeAgentId)?.name ?? null)
                : null
            }
          />
        </div>
        <IssueDescription
          description={issue.description ?? ""}
          onSave={(description) => mutations.update(issueId, { description })}
        />
        <CommentThread
          issue={issue}
          comments={comments}
          agents={agents}
          members={members}
          onAdd={async (body) => {
            await mutations.addComment(issueId, body);
          }}
          onDeleteComment={async (commentId) => {
            await del(`/issues/${issueId}/comments/${commentId}`);
          }}
          composerAccessory={
            hasVisibleMonitorSurface(issue) ? (
              <IssueMonitorComposerStrip
                issue={issue}
                onCheckNow={checkMonitorNow}
                checkingNow={checkingMonitorNow}
              />
            ) : null
          }
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
          onSkipInteraction={async (interaction) => {
            await interactionMutations.skip(issueId, interaction.id);
          }}
          onSubmitInteractionVerdicts={async (interaction, verdicts) => {
            await interactionMutations.submitVerdicts(issueId, interaction.id, { verdicts });
          }}
          feedbackVotes={votesByTarget}
          onVote={async (commentId, vote, options) => {
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
        <IssueContinuationHandoff document={continuationHandoff} />
        <IssueScheduledRetryCard issueId={issue.id} scheduledRetry={issue.scheduledRetry ?? null} />
      </main>
      <aside className="flex flex-col gap-6 min-w-0">
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
            updateIssue({
              assigneeAgentId,
              assigneeUserId: null,
            });
          }}
          onAssigneeUserChange={(assigneeUserId) => {
            // Picking a human clears any agent assignee in the same PATCH.
            updateIssue({
              assigneeUserId,
              assigneeAgentId: null,
            });
          }}
          onProjectChange={(projectId) => {
            updateIssue({ projectId });
          }}
          onGoalChange={(goalId) => {
            updateIssue({ goalId });
          }}
          onBlockedByChange={(blockedByIssueIds) => {
            updateIssue({ blockedByIssueIds });
          }}
          onLabelsChange={(labelIds) => {
            updateIssue({ labelIds });
          }}
          onParentChange={(parentId) => {
            updateIssue({ parentId });
          }}
          onMonitorChange={(monitor) => {
            // executionPolicy bundles monitor + reviewer/approver stages under
            // REPLACE-on-the-wire semantics, so editing the monitor must rebuild
            // the WHOLE policy from the issue's current one — preserving any
            // stages — rather than sending a bare { monitor }. patchExecutionPolicy
            // is the single read-modify-write that guarantees this.
            updateIssue({
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                monitor,
              }),
            });
          }}
          onMonitorClear={() => {
            // Clearing the monitor preserves any reviewer/approver stages; the
            // helper returns null only when nothing else remains.
            updateIssue({
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                monitor: null,
              }),
            });
          }}
          onReviewersChange={(reviewers) => {
            updateIssue({
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                reviewers,
              }),
            });
          }}
          onApproversChange={(approvers) => {
            updateIssue({
              executionPolicy: patchExecutionPolicy(issue.executionPolicy, {
                approvers,
              }),
            });
          }}
          onCheckNow={(id) => mutations.monitorCheckNow(id)}
          onWorkModeChange={(workMode) => {
            updateIssue({ workMode });
          }}
          // The watchdog has its own upstream routes (not the issue PATCH); the
          // detail read carries it back as issue.watchdog. A rejected write is
          // left to the row, which shows it inside its editor. The write counts
          // as done once it resolves: the refresh after it is fired and its
          // failure swallowed, or a save that landed would read as failed and a
          // retry would re-send the PUT (re-running upstream's evaluation).
          onWatchdogSave={async (watchdog) => {
            await put(`/issues/${issueId}/watchdog`, watchdog);
            void mutate(`/teams${teamsQueryKeys.issues.detail(issueId)}`).catch(() => {});
          }}
          onWatchdogRemove={async () => {
            await del(`/issues/${issueId}/watchdog`);
            void mutate(`/teams${teamsQueryKeys.issues.detail(issueId)}`).catch(() => {});
          }}
        />
        {/* Stock places the Cases rail at the foot of the properties pane; it
            renders nothing when no cases are linked (or Cases is off). */}
        <IssueCasesPanel issueId={issue.id} />
        <SubIssuesSection
          issueId={issueId}
          agents={agents}
          members={members}
          projects={projects}
          parentLabel={issue.identifier ? `${issue.identifier} ${issue.title}` : issue.title}
        />
        <IssueApprovalsSection issueId={issueId} />
        <IssueRunLedger
          issueId={issueId}
          issueStatus={issue.status}
          agents={agents}
          members={members}
        />
        <IssueTreeControlSection issueId={issueId} />
        <IssueDocumentsSection issueId={issueId} agents={agents} />
        <IssueWorkProductsSection issueId={issueId} />
        <IssueAttachmentsSection issueId={issueId} />
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
        <p className="text-ui" style={{ color: "var(--midnight-soft)" }}>
          {issue.identifier ? `${issue.identifier}: ` : ""}
          {issue.title}
        </p>
      </EditorialDialog>
    </div>
    </AttachmentIssueContext.Provider>
  );
}
