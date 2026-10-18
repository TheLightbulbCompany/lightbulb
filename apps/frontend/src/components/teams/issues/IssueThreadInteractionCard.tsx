// apps/frontend/src/components/teams/issues/IssueThreadInteractionCard.tsx

// Ported from upstream Paperclip's IssueThreadInteractionCardClassic.tsx
// (the lower-chrome "classic" variant — MIT, © 2025 Paperclip AI), re-themed
// onto the Teams editorial cream/forest palette. All four interaction kinds
// (suggest_tasks, ask_user_questions, request_confirmation,
// request_checkbox_confirmation) with their pending + resolved states;
// request_item_verdicts shows only its prompt + progress (the task chat owns it).
//
// Re-theme notes vs upstream:
//   - MarkdownBody          -> TaskMarkdown (shared editorial renderer + issue links)
//   - shadcn Button         -> EditorialButton (default/cta -> primary,
//                              outline -> secondary, ghost -> ghost)
//   - Tooltip on the header dropped; created-by / date render bare
//   - `sky` selection accent recolored to --midnight / --cream-deep
//   - emerald / rose / amber status hues kept (they read fine on cream)
//   - semantic shadcn tokens (foreground/background/border/muted) mapped to
//     editorial CSS vars (--midnight / --cream-deep / --edge / --midnight-muted …)
//   - Link -> next/link via createIssueDetailPath for created-task links;
//     confirmation-target chips that point at in-issue document anchors (no
//     Teams route) render as bare spans rather than dead links.

"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  GitBranch,
  ListChecks,
  Loader2,
  MessageSquareQuote,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { EditorialButton } from "@/components/teams/shell";
import { TaskMarkdown } from "@/components/teams/shared/components/TaskMarkdown";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { formatAssigneeUserLabel } from "@/components/teams/shared/lib/assignees";
import { createIssueDetailPath } from "@/components/teams/shared/lib/issueDetailBreadcrumb";
import { handleShellLinkClick } from "@/lib/shellNav";
import {
  buildIssueThreadInteractionSummary,
  buildSuggestedTaskTree,
  collectSuggestedTaskClientKeys,
  countSuggestedTaskNodes,
  getCheckboxConfirmationSelectedLabels,
  getQuestionAnswerLabels,
  getRequestConfirmationTargetHref,
  type SuggestedTaskTreeNode,
} from "./lib/issue-thread-interactions";
import type {
  AskUserQuestionsAnswer,
  AskUserQuestionsInteraction,
  CompanyAgent,
  IssueThreadInteraction,
  RequestCheckboxConfirmationInteraction,
  RequestConfirmationInteraction,
  RequestConfirmationResult,
  RequestConfirmationTarget,
  SuggestTasksInteraction,
  SuggestTasksResultCreatedTask,
} from "@/components/teams/shared/types";
import { notifyApiError } from "@/lib/notify";

const OTHER_ANSWER_ID = "__isol8_other__";

export interface IssueThreadInteractionCardProps {
  interaction: IssueThreadInteraction;
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
  onAcceptInteraction?: (
    interaction:
      | SuggestTasksInteraction
      | RequestConfirmationInteraction
      | RequestCheckboxConfirmationInteraction,
    selectedClientKeys?: string[],
    selectedOptionIds?: string[],
  ) => Promise<void> | void;
  onRejectInteraction?: (
    interaction:
      | SuggestTasksInteraction
      | RequestConfirmationInteraction
      | RequestCheckboxConfirmationInteraction,
    reason?: string,
  ) => Promise<void> | void;
  onSubmitInteractionAnswers?: (
    interaction: AskUserQuestionsInteraction,
    answers: AskUserQuestionsAnswer[],
  ) => Promise<void> | void;
  onCancelInteraction?: (
    interaction: AskUserQuestionsInteraction,
  ) => Promise<void> | void;
}

function formatShortDate(value: Date | string | null | undefined): string {
  if (!value) return "";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function resolveActorLabel(args: {
  agentId?: string | null;
  userId?: string | null;
  /** BFF-enriched email for the human actor (userId is a Paperclip principal id). */
  email?: string | null;
  /** BFF-enriched Clerk id twin — matches currentUserId for the viewer. */
  clerkUserId?: string | null;
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
}) {
  const { agentId, userId, email, clerkUserId, agentMap, currentUserId } = args;
  if (agentId) {
    return agentMap?.get(agentId)?.name ?? agentId.slice(0, 8);
  }
  if (userId) {
    if (clerkUserId && currentUserId && clerkUserId === currentUserId) return "You";
    if (email) return email;
    return formatAssigneeUserLabel(userId, currentUserId, null) ?? "User";
  }
  return "Unknown";
}

function statusLabel(status: IssueThreadInteraction["status"]) {
  switch (status) {
    case "pending":
      return "Pending";
    case "accepted":
      return "Accepted";
    case "rejected":
      return "Rejected";
    case "answered":
      return "Answered";
    case "cancelled":
      return "Cancelled";
    case "expired":
      return "Expired";
    case "failed":
      return "Failed";
    default:
      return status;
  }
}

function interactionKindLabel(kind: IssueThreadInteraction["kind"]) {
  switch (kind) {
    case "suggest_tasks":
      return "Suggested tasks";
    case "ask_user_questions":
      return "Ask user questions";
    case "request_confirmation":
      return "Confirmation";
    case "request_checkbox_confirmation":
      return "Checkbox confirmation";
    case "request_item_verdicts":
      return "Review items";
    default:
      return kind;
  }
}

// Renders the status glyph directly (rather than upstream's
// `const Icon = statusIcon(status)` + `<Icon />`, which the
// react-hooks/static-components lint flags as a component created during render).
function StatusGlyph({
  status,
  className,
}: {
  status: IssueThreadInteraction["status"];
  className?: string;
}) {
  switch (status) {
    case "accepted":
    case "answered":
      return <CheckCircle2 className={className} />;
    case "rejected":
    case "cancelled":
    case "failed":
      return <XCircle className={className} />;
    case "expired":
      return <AlertTriangle className={className} />;
    default:
      return <CircleDashed className={className} />;
  }
}

function statusClasses(status: IssueThreadInteraction["status"]) {
  switch (status) {
    case "accepted":
    case "answered":
      return {
        shell: "border-ok bg-transparent",
        badge:
          "border-ok bg-cream text-ok",
      };
    case "rejected":
    case "cancelled":
      return {
        shell: "border-err bg-transparent",
        badge: "border-err bg-cream text-err",
      };
    case "failed":
    case "expired":
      return {
        shell: "border-warn bg-transparent",
        badge: "border-warn bg-cream text-warn",
      };
    default:
      // Pending — recolored from upstream `sky` to the editorial forest accent.
      return {
        shell: "border-[var(--midnight)] bg-transparent",
        badge:
          "border-[var(--midnight)] bg-[var(--cream-deep)] text-[var(--midnight)]",
      };
  }
}

function TaskField({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "subtle";
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm border px-2 py-0.5 label-caps",
        tone === "default"
          ? "border-[var(--edge)] bg-transparent text-[var(--midnight)]"
          : "border-[var(--edge)] bg-transparent text-[var(--midnight-muted)]",
      )}
    >
      {label}: {value}
    </span>
  );
}

function createdTaskMap(
  createdTasks: readonly SuggestTasksResultCreatedTask[] | undefined,
) {
  return new Map(
    (createdTasks ?? []).map((entry) => [entry.clientKey, entry] as const),
  );
}

function TaskTreeNode({
  node,
  createdByClientKey,
  agentMap,
  currentUserId,
  depth = 0,
  selectedClientKeys,
  skippedClientKeys,
  showSelection,
  onToggleSelection,
}: {
  node: SuggestedTaskTreeNode;
  createdByClientKey: ReadonlyMap<string, SuggestTasksResultCreatedTask>;
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
  depth?: number;
  selectedClientKeys?: ReadonlySet<string>;
  skippedClientKeys?: ReadonlySet<string>;
  showSelection?: boolean;
  onToggleSelection?: (node: SuggestedTaskTreeNode, checked: boolean) => void;
}) {
  const visibleChildren = node.children.filter((child) => !child.task.hiddenInPreview);
  const hiddenChildCount = node.children
    .filter((child) => child.task.hiddenInPreview)
    .reduce((sum, child) => sum + countSuggestedTaskNodes(child), 0);
  const createdTask = createdByClientKey.get(node.task.clientKey);
  const createdTaskUrl = createdTask?.issueId
    ? createIssueDetailPath(createdTask.issueId)
    : null;
  const isSelected = selectedClientKeys?.has(node.task.clientKey) ?? false;
  const isSkipped = skippedClientKeys?.has(node.task.clientKey) ?? false;
  const assigneeLabel = resolveActorLabel({
    agentId: node.task.assigneeAgentId,
    userId: node.task.assigneeUserId,
    email: node.task.assigneeEmail,
    clerkUserId: node.task.assigneeClerkUserId,
    agentMap,
    currentUserId,
  });
  const hasExplicitAssignee = Boolean(
    node.task.assigneeAgentId || node.task.assigneeUserId,
  );
  const labels = node.task.labels ?? [];
  const hasMetadata = hasExplicitAssignee
    || Boolean(node.task.billingCode)
    || Boolean(node.task.projectId)
    || labels.length > 0;

  return (
    <>
      <div
        className={cn(
          "relative border-b border-[var(--edge)] px-3 py-2.5 last:border-b-0",
          depth > 0 && "before:absolute before:left-3 before:top-0 before:h-full before:w-px before:bg-[var(--edge)]",
        )}
        style={depth > 0 ? { paddingLeft: `${depth * 24 + 12}px` } : undefined}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-start gap-2">
              {showSelection ? (
                <Checkbox
                  checked={isSelected}
                  onCheckedChange={(checked) => onToggleSelection?.(node, checked === true)}
                  aria-label={`Include ${node.task.title}`}
                  className="mt-0.5 border-[var(--edge)] data-[state=checked]:border-[var(--midnight)] data-[state=checked]:bg-[var(--midnight)] data-[state=checked]:text-[var(--cream)]"
                />
              ) : null}
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-1.5">
                  {node.task.priority ? (
                    <PriorityIcon
                      priority={node.task.priority}
                      className="mt-px"
                    />
                  ) : null}
                  <div className="min-w-0 truncate text-ui font-medium text-[var(--midnight)]">
                    {node.task.title}
                  </div>
                </div>
                {depth > 0 ? (
                  <div className="mt-0.5 label-caps text-[var(--midnight-muted)]">
                    Child task
                  </div>
                ) : null}
                {node.task.description ? (
                  <p className="mt-0.5 text-ui leading-5 text-[var(--midnight-muted)]">
                    {node.task.description}
                  </p>
                ) : null}
              </div>
            </div>
          </div>

          {createdTask?.issueId && createdTaskUrl ? (
            <Link
              href={createdTaskUrl}
              prefetch={false}
              onClick={(e) => handleShellLinkClick(e, createdTaskUrl)}
              className="inline-flex shrink-0 items-center gap-1 rounded-sm border border-ok bg-cream px-2.5 py-1 text-caption font-medium text-ok transition-colors hover:bg-cream-deep"
            >
              {createdTask.identifier ?? createdTask.issueId.slice(0, 8)}
              <ChevronRight size={12} strokeWidth={1.75} />
            </Link>
          ) : isSkipped ? (
            <span className="inline-flex shrink-0 items-center rounded-sm border border-warn bg-cream px-2.5 py-1 text-caption font-medium text-warn">
              Skipped
            </span>
          ) : null}
        </div>

        {hasMetadata ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {hasExplicitAssignee ? (
              <TaskField label="Assignee" value={assigneeLabel} />
            ) : null}
            {node.task.billingCode ? (
              <TaskField label="Billing" value={node.task.billingCode} />
            ) : null}
            {node.task.projectId ? (
              <TaskField label="Project" value={node.task.projectId} tone="subtle" />
            ) : null}
            {labels.map((label) => (
              <TaskField key={label} label="Label" value={label} tone="subtle" />
            ))}
          </div>
        ) : null}

        {hiddenChildCount > 0 ? (
          <div className="mt-2 flex items-center gap-2 rounded-lg border border-warn bg-cream px-3 py-2 text-ui text-warn">
            <GitBranch size={16} className="shrink-0" />
            <span>
              {hiddenChildCount === 1
                ? "1 follow-on task hidden in preview"
                : `${hiddenChildCount} follow-on tasks hidden in preview`}
            </span>
          </div>
        ) : null}
      </div>

      {visibleChildren.length > 0 ? (
        <>
          {visibleChildren.map((child) => (
            <TaskTreeNode
              key={child.task.clientKey}
              node={child}
              createdByClientKey={createdByClientKey}
              agentMap={agentMap}
              currentUserId={currentUserId}
              depth={depth + 1}
              selectedClientKeys={selectedClientKeys}
              skippedClientKeys={skippedClientKeys}
              showSelection={showSelection}
              onToggleSelection={onToggleSelection}
            />
          ))}
        </>
      ) : null}
    </>
  );
}

function SuggestTasksCard({
  interaction,
  agentMap,
  currentUserId,
  onAcceptInteraction,
  onRejectInteraction,
}: {
  interaction: SuggestTasksInteraction;
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
  onAcceptInteraction?: (
    interaction: SuggestTasksInteraction,
    selectedClientKeys?: string[],
  ) => Promise<void> | void;
  onRejectInteraction?: (
    interaction: SuggestTasksInteraction,
    reason?: string,
  ) => Promise<void> | void;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [working, setWorking] = useState<"accept" | "reject" | null>(null);
  const [rejectReason, setRejectReason] = useState(
    interaction.result?.rejectionReason ?? "",
  );

  useEffect(() => {
    setRejectReason(interaction.result?.rejectionReason ?? "");
    if (interaction.status !== "pending") {
      setRejecting(false);
      setWorking(null);
    }
  }, [interaction.result?.rejectionReason, interaction.status]);

  const roots = useMemo(
    () =>
      buildSuggestedTaskTree(interaction.payload.tasks).filter(
        (node) => !node.task.hiddenInPreview,
      ),
    [interaction.payload.tasks],
  );
  const createdByClientKey = useMemo(
    () => createdTaskMap(interaction.result?.createdTasks),
    [interaction.result?.createdTasks],
  );
  const skippedClientKeys = useMemo(
    () => new Set(interaction.result?.skippedClientKeys ?? []),
    [interaction.result?.skippedClientKeys],
  );
  const totalTasks = interaction.payload.tasks.length;
  const [selectedClientKeys, setSelectedClientKeys] = useState<Set<string>>(
    () => new Set(interaction.payload.tasks.map((task) => task.clientKey)),
  );
  const taskSelectionSeed = useMemo(
    () => interaction.payload.tasks.map((task) => task.clientKey).join("\n"),
    [interaction.payload.tasks],
  );

  useEffect(() => {
    setSelectedClientKeys(new Set(interaction.payload.tasks.map((task) => task.clientKey)));
  }, [interaction.id, interaction.status, taskSelectionSeed, interaction.payload.tasks]);

  const taskByClientKey = useMemo(
    () => new Map(interaction.payload.tasks.map((task) => [task.clientKey, task] as const)),
    [interaction.payload.tasks],
  );
  const selectedCount = selectedClientKeys.size;
  const createdCount = interaction.result?.createdTasks?.length ?? 0;
  const skippedCount = interaction.result?.skippedClientKeys?.length ?? 0;

  async function handleAccept() {
    if (!onAcceptInteraction) return;
    setWorking("accept");
    try {
      await onAcceptInteraction(interaction, [...selectedClientKeys]);
    } catch (err) {
      notifyApiError(err, "Couldn't accept the suggested tasks");
    } finally {
      setWorking(null);
    }
  }

  async function handleReject() {
    if (!onRejectInteraction) return;
    setWorking("reject");
    try {
      await onRejectInteraction(interaction, rejectReason.trim() || undefined);
      setRejecting(false);
    } catch (err) {
      notifyApiError(err, "Couldn't reject the suggested tasks");
    } finally {
      setWorking(null);
    }
  }

  function handleToggleSelection(node: SuggestedTaskTreeNode, checked: boolean) {
    const subtreeClientKeys = collectSuggestedTaskClientKeys(node);
    setSelectedClientKeys((current) => {
      const next = new Set(current);
      if (!checked) {
        for (const clientKey of subtreeClientKeys) {
          next.delete(clientKey);
        }
        return next;
      }

      for (const clientKey of subtreeClientKeys) {
        next.add(clientKey);
      }

      let parentClientKey = taskByClientKey.get(node.task.clientKey)?.parentClientKey ?? null;
      while (parentClientKey) {
        next.add(parentClientKey);
        parentClientKey = taskByClientKey.get(parentClientKey)?.parentClientKey ?? null;
      }
      return next;
    });
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-caption text-[var(--midnight-muted)]">
        <span>{totalTasks === 1 ? "1 draft issue" : `${totalTasks} draft issues`}</span>
        {interaction.payload.defaultParentId ? (
          <TaskField label="Default parent" value={interaction.payload.defaultParentId} tone="subtle" />
        ) : null}
      </div>

      <div className="overflow-hidden border border-[var(--edge)]">
        {roots.map((root) => (
          <TaskTreeNode
            key={root.task.clientKey}
            node={root}
            createdByClientKey={createdByClientKey}
            agentMap={agentMap}
            currentUserId={currentUserId}
            selectedClientKeys={selectedClientKeys}
            skippedClientKeys={skippedClientKeys}
            showSelection={interaction.status === "pending"}
            onToggleSelection={handleToggleSelection}
          />
        ))}
      </div>

      {interaction.status === "accepted" ? (
        <div className="rounded-lg border border-ok bg-cream px-3 py-2 text-ui text-ok">
          <div className="label-caps text-ok">
            Resolution summary
          </div>
          <p className="mt-1 leading-6">
            {skippedCount > 0
              ? `Created ${createdCount} draft ${createdCount === 1 ? "issue" : "issues"} and skipped ${skippedCount} during review.`
              : `Created all ${createdCount} draft ${createdCount === 1 ? "issue" : "issues"}.`}
          </p>
        </div>
      ) : null}

      {interaction.status === "rejected" ? (
        <div className="rounded-lg border border-err bg-cream px-3 py-2 text-ui text-err">
          <div className="label-caps text-err">
            Rejection reason
          </div>
          <p className={cn(
            "mt-1 leading-6",
            !interaction.result?.rejectionReason && "text-err/75",
          )}>
            {interaction.result?.rejectionReason || "No reason provided."}
          </p>
        </div>
      ) : null}

      {interaction.status === "pending" ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2 text-caption text-[var(--midnight-muted)]">
              <span>
                {selectedCount === totalTasks
                  ? `All ${totalTasks} draft ${totalTasks === 1 ? "issue" : "issues"} selected`
                  : `${selectedCount} of ${totalTasks} draft ${totalTasks === 1 ? "issue" : "issues"} selected`}
              </span>
              {selectedCount < totalTasks ? (
                <span>
                  {totalTasks - selectedCount} will be skipped if you accept this interaction.
                </span>
              ) : null}
            </div>

            <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
              <EditorialButton
                type="button"
                variant="primary"
                size="sm"
                disabled={!onAcceptInteraction || working !== null || selectedCount === 0}
                onClick={() => void handleAccept()}
              >
                {working === "accept" ? (
                  <>
                    <Loader2 size={16} className="mr-2 animate-spin" />
                    Accepting...
                  </>
                ) : (
                  selectedCount === totalTasks ? "Accept drafts" : "Accept selected drafts"
                )}
              </EditorialButton>
              <EditorialButton
                type="button"
                variant="secondary"
                size="sm"
                disabled={!onRejectInteraction || working !== null}
                onClick={() => setRejecting((current) => !current)}
              >
                Reject
              </EditorialButton>
              {selectedCount < totalTasks ? (
                <EditorialButton
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={working !== null}
                  onClick={() => setSelectedClientKeys(new Set(interaction.payload.tasks.map((task) => task.clientKey)))}
                >
                  Reset selection
                </EditorialButton>
              ) : null}
            </div>
          </div>

          {rejecting ? (
            <div className="space-y-3">
              <Textarea
                value={rejectReason}
                onChange={(event) => setRejectReason(event.target.value)}
                placeholder="Add a short reason for rejecting this suggestion"
                className="min-h-24 text-ui"
                style={{
                  background: "var(--cream-deep)",
                  border: "1px solid var(--edge)",
                  color: "var(--midnight)",
                }}
              />
              <div className="flex justify-end">
                <EditorialButton
                  type="button"
                  variant="secondary"
                  size="sm"
                  disabled={!onRejectInteraction || working !== null}
                  onClick={() => void handleReject()}
                >
                  {working === "reject" ? (
                    <>
                      <Loader2 size={16} className="mr-2 animate-spin" />
                      Saving...
                    </>
                  ) : (
                    "Save rejection"
                  )}
                </EditorialButton>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function QuestionOptionButton({
  id,
  label,
  description,
  selected,
  selectionMode,
  onClick,
}: {
  id: string;
  label: string;
  description?: string | null;
  selected: boolean;
  selectionMode: "single" | "multi";
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role={selectionMode === "single" ? "radio" : "checkbox"}
      aria-checked={selected}
      className={cn(
        "w-full rounded-sm border px-4 py-3 text-left transition-colors",
        selected
          ? "border-[var(--midnight)] bg-[var(--cream-deeper)] text-[var(--midnight)]"
          : "border-[var(--edge)] bg-transparent text-[var(--midnight)] hover:border-[var(--midnight)] hover:bg-[var(--cream-deep)]",
      )}
      id={id}
      onClick={onClick}
    >
      <div className="text-ui font-medium text-[var(--midnight)]">
        {label}
      </div>
      {description ? (
        <div
          className={cn(
            "mt-1 text-ui leading-6",
            selected ? "text-[var(--midnight-soft)]" : "text-[var(--midnight-muted)]",
          )}
        >
          {description}
        </div>
      ) : null}
    </button>
  );
}

function AskUserQuestionsCard({
  interaction,
  onSubmitInteractionAnswers,
  onCancelInteraction,
}: {
  interaction: AskUserQuestionsInteraction;
  onSubmitInteractionAnswers?: (
    interaction: AskUserQuestionsInteraction,
    answers: AskUserQuestionsAnswer[],
  ) => Promise<void> | void;
  onCancelInteraction?: (
    interaction: AskUserQuestionsInteraction,
  ) => Promise<void> | void;
}) {
  const [draftAnswers, setDraftAnswers] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(
      (interaction.result?.answers ?? []).map((answer) => [
        answer.questionId,
        [...answer.optionIds],
      ]),
    ),
  );
  const [draftOtherAnswers, setDraftOtherAnswers] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      (interaction.result?.answers ?? [])
        .filter((answer) => answer.otherText)
        .map((answer) => [answer.questionId, answer.otherText ?? ""]),
    ),
  );
  const [otherActiveQuestions, setOtherActiveQuestions] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      (interaction.result?.answers ?? [])
        .filter((answer) => answer.otherText)
        .map((answer) => [answer.questionId, true]),
    ),
  );
  const [working, setWorking] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    setDraftAnswers(
      Object.fromEntries(
        (interaction.result?.answers ?? []).map((answer) => [
          answer.questionId,
          [...answer.optionIds],
        ]),
      ),
    );
    setDraftOtherAnswers(
      Object.fromEntries(
        (interaction.result?.answers ?? [])
          .filter((answer) => answer.otherText)
          .map((answer) => [answer.questionId, answer.otherText ?? ""]),
      ),
    );
    setOtherActiveQuestions(
      Object.fromEntries(
        (interaction.result?.answers ?? [])
          .filter((answer) => answer.otherText)
          .map((answer) => [answer.questionId, true]),
      ),
    );
  }, [interaction.result?.answers]);

  const questions = interaction.payload.questions;
  const requiredQuestions = questions.filter((question) => question.required);
  const canSubmit = requiredQuestions.every(
    (question) =>
      (draftAnswers[question.id] ?? []).length > 0
      || (
        otherActiveQuestions[question.id] === true
        && (draftOtherAnswers[question.id]?.trim().length ?? 0) > 0
      ),
  );

  function toggleOption(questionId: string, optionId: string, selectionMode: "single" | "multi") {
    if (optionId === OTHER_ANSWER_ID) {
      setOtherActiveQuestions((current) => ({
        ...current,
        [questionId]: !current[questionId],
      }));
      if (selectionMode === "single") {
        setDraftAnswers((current) => ({ ...current, [questionId]: [] }));
      }
      return;
    }

    setDraftAnswers((current) => {
      const existing = current[questionId] ?? [];
      if (selectionMode === "single") {
        return { ...current, [questionId]: [optionId] };
      }
      const next = existing.includes(optionId)
        ? existing.filter((value) => value !== optionId)
        : [...existing, optionId];
      return { ...current, [questionId]: next };
    });
    if (selectionMode === "single") {
      setOtherActiveQuestions((current) => ({ ...current, [questionId]: false }));
    }
  }

  async function handleSubmit() {
    if (!onSubmitInteractionAnswers || !canSubmit) return;
    setWorking(true);
    try {
      await onSubmitInteractionAnswers(
        interaction,
        questions.map((question) => {
          const otherText = otherActiveQuestions[question.id] === true
            ? draftOtherAnswers[question.id]?.trim() ?? ""
            : "";
          return {
            questionId: question.id,
            optionIds: draftAnswers[question.id] ?? [],
            ...(otherText ? { otherText } : {}),
          };
        }),
      );
    } catch (err) {
      notifyApiError(err, "Couldn't send your answers");
    } finally {
      setWorking(false);
    }
  }

  async function handleCancel() {
    if (!onCancelInteraction) return;
    setCancelling(true);
    try {
      await onCancelInteraction(interaction);
    } catch (err) {
      notifyApiError(err, "Couldn't cancel the questions");
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-caption text-[var(--midnight-muted)]">
        <span className="inline-flex items-center gap-1 rounded-full border border-[var(--edge)] bg-[var(--cream-deep)] px-2.5 py-1 label-caps text-[var(--midnight-soft)]">
          <MessageSquareQuote size={12} strokeWidth={1.75} />
          Ask user questions
        </span>
        <span>
          {questions.length === 1
            ? "1 question"
            : `${questions.length} questions`}
        </span>
      </div>

      {interaction.status === "pending" ? (
        <div className="space-y-4">
          {questions.map((question, index) => (
            <div
              key={question.id}
              className="rounded-xl border border-[var(--edge)] bg-[var(--cream-deep)] p-4"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="label-caps text-[var(--midnight-muted)]">
                    Question {index + 1}
                  </div>
                  <div
                    id={`${interaction.id}-${question.id}-prompt`}
                    className="mt-1 text-ui font-semibold text-[var(--midnight)]"
                  >
                    {question.prompt}
                  </div>
                  {question.helpText ? (
                    <p className="mt-1 text-ui leading-6 text-[var(--midnight-muted)]">
                      {question.helpText}
                    </p>
                  ) : null}
                </div>
                <TaskField
                  label={question.selectionMode === "single" ? "Pick" : "Pick many"}
                  value={question.required ? "Required" : "Optional"}
                  tone="subtle"
                />
              </div>

              <div className="mt-3 space-y-3">
                <div
                  className="grid gap-3"
                  role={question.selectionMode === "single" ? "radiogroup" : "group"}
                  aria-labelledby={`${interaction.id}-${question.id}-prompt`}
                >
                  {question.options.map((option) => (
                    <QuestionOptionButton
                      key={option.id}
                      id={`${interaction.id}-${question.id}-${option.id}`}
                      label={option.label}
                      description={option.description}
                      selected={(draftAnswers[question.id] ?? []).includes(option.id)}
                      selectionMode={question.selectionMode}
                      onClick={() =>
                        toggleOption(question.id, option.id, question.selectionMode)}
                    />
                  ))}
                </div>
                <button
                  type="button"
                  id={`${interaction.id}-${question.id}-other`}
                  aria-expanded={otherActiveQuestions[question.id] === true}
                  className={cn(
                    "text-ui font-medium underline underline-offset-4 transition-colors",
                    otherActiveQuestions[question.id]
                      ? "text-[var(--midnight)] hover:text-[var(--midnight)]"
                      : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
                  )}
                  onClick={() =>
                    toggleOption(question.id, OTHER_ANSWER_ID, question.selectionMode)}
                >
                  Other
                </button>
                {otherActiveQuestions[question.id] ? (
                  <Textarea
                    aria-label={`Other answer for ${question.prompt}`}
                    value={draftOtherAnswers[question.id] ?? ""}
                    onChange={(event) =>
                      setDraftOtherAnswers((current) => ({
                        ...current,
                        [question.id]: event.target.value,
                      }))}
                    placeholder="Type your answer"
                    className="min-h-24 text-ui"
                    style={{
                      background: "var(--cream-deep)",
                      border: "1px solid var(--edge)",
                      color: "var(--midnight)",
                    }}
                  />
                ) : null}
              </div>
            </div>
          ))}

          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--edge)] bg-[var(--cream-deep)] p-4">
            <div className="text-ui text-[var(--midnight-muted)]">
              Submit once after you finish the full form.
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {onCancelInteraction ? (
                <EditorialButton
                  type="button"
                  variant="secondary"
                  size="sm"
                  disabled={working || cancelling}
                  onClick={() => void handleCancel()}
                >
                  {cancelling ? (
                    <>
                      <Loader2 size={16} className="mr-2 animate-spin" />
                      Cancelling...
                    </>
                  ) : (
                    "Cancel question"
                  )}
                </EditorialButton>
              ) : null}
              <EditorialButton
                type="button"
                variant="primary"
                size="sm"
                disabled={!onSubmitInteractionAnswers || !canSubmit || working || cancelling}
                onClick={() => void handleSubmit()}
              >
                {working ? (
                  <>
                    <Loader2 size={16} className="mr-2 animate-spin" />
                    Submitting...
                  </>
                ) : (
                  interaction.payload.submitLabel ?? "Submit answers"
                )}
              </EditorialButton>
            </div>
          </div>
        </div>
      ) : interaction.status === "cancelled" ? (
        <div className="rounded-lg border border-err bg-cream px-3 py-2 text-ui leading-6 text-err">
          <div className="font-semibold">Question cancelled</div>
          {interaction.result?.cancellationReason ? (
            <p className="mt-1">{interaction.result.cancellationReason}</p>
          ) : (
            <p className="mt-1">No answer was recorded.</p>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          {questions.map((question) => {
            const labels = getQuestionAnswerLabels({
              question,
              answers: interaction.result?.answers ?? [],
            });
            return (
              <div
                key={question.id}
                className="rounded-xl border border-[var(--edge)] bg-[var(--cream-deep)] p-4"
              >
                <div className="text-ui font-semibold text-[var(--midnight)]">
                  {question.prompt}
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {labels.length > 0 ? (
                    labels.map((label) => (
                      <TaskField key={label} label="Answer" value={label} />
                    ))
                  ) : (
                    <span className="text-ui text-[var(--midnight-muted)]">No answer recorded.</span>
                  )}
                </div>
              </div>
            );
          })}

          {interaction.result?.summaryMarkdown ? (
            <div className="rounded-lg border border-ok bg-cream px-3 py-2">
              <div className="mb-2 label-caps text-ok">
                Submitted summary
              </div>
              <TaskMarkdown body={interaction.result.summaryMarkdown} />
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function requestConfirmationTargetLabel(target: RequestConfirmationTarget) {
  if (target.label) return target.label;
  const revision = target.revisionNumber ? ` v${target.revisionNumber}` : "";
  if (target.type === "issue_document" && target.key === "plan") {
    return `Plan${revision}`;
  }
  return `${target.key}${revision}`;
}

function RequestConfirmationTargetChip({
  issueId,
  target,
  tone = "default",
}: {
  issueId: string;
  target: RequestConfirmationTarget | null | undefined;
  tone?: "default" | "subtle";
}) {
  if (!target) return null;

  const href = getRequestConfirmationTargetHref({ issueId, target });
  const isExternal = Boolean(href && /^https?:\/\//i.test(href));
  // Same-issue document targets resolve to a bare `#document-<key>` anchor — an
  // in-page link that scrolls to (and expands) the document row, which now
  // renders `id="document-<key>"`. This is what fixes the previously dead chip.
  const isInPageAnchor = Boolean(href && href.startsWith("#"));
  const className = cn(
    "inline-flex max-w-full items-center gap-1.5 rounded-sm border px-2 py-0.5 label-caps",
    tone === "default"
      ? "border-[var(--edge)] bg-transparent text-[var(--midnight)]"
      : "border-[var(--edge)] bg-transparent text-[var(--midnight-muted)]",
    (isExternal || isInPageAnchor)
      && "transition-colors hover:border-[var(--midnight)] hover:bg-[var(--cream-deep)]",
  );
  const content = (
    <>
      <GitBranch size={12} strokeWidth={1.75} className="shrink-0" />
      <span className="min-w-0 truncate">{requestConfirmationTargetLabel(target)}</span>
    </>
  );

  // External https targets open in a new tab; same-issue document anchors are
  // in-page links. Cross-issue document targets (a full `/issues/…` path with
  // no Teams route) still render bare.
  if (isExternal) {
    return (
      <a href={href!} target="_blank" rel="noreferrer" className={className}>
        {content}
      </a>
    );
  }
  if (isInPageAnchor) {
    return (
      <a href={href!} className={className}>
        {content}
      </a>
    );
  }
  return <span className={className}>{content}</span>;
}

function ConfirmationDeclinedBlock({
  issueId,
  result,
  target,
}: {
  issueId: string;
  result: RequestConfirmationResult | null | undefined;
  target: RequestConfirmationTarget | null;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-ui leading-6 text-[var(--midnight)]">
        <span className="font-medium">Declined</span>
        <RequestConfirmationTargetChip issueId={issueId} target={target} />
      </div>
      {result?.reason ? (
        <blockquote className="rounded-lg border-l-2 border-err bg-cream px-3 py-2 text-ui leading-6 text-err">
          {result.reason}
        </blockquote>
      ) : null}
    </div>
  );
}

function ConfirmationExpiredBlock({
  issueId,
  result,
  target,
}: {
  issueId: string;
  result: RequestConfirmationResult | null | undefined;
  target: RequestConfirmationTarget | null;
}) {
  const outcome = result?.outcome;
  const staleTarget = result?.staleTarget ?? null;
  const expiredByComment = outcome === "superseded_by_comment";
  const expiredByTargetChange = outcome === "stale_target";
  return (
    <div className="space-y-3 rounded-lg border border-warn bg-cream px-3 py-2 text-ui text-warn">
      <div className="label-caps text-warn">
        {expiredByComment ? "Expired by comment" : "Expired by target change"}
      </div>
      <p className="leading-6">
        {expiredByComment
          ? "A board comment superseded this request before it was resolved."
          : "The requested target changed before this request was resolved."}
      </p>
      {expiredByComment && result?.commentId ? (
        <a
          href={`#comment-${result.commentId}`}
          className="border border-warn inline-flex h-7 items-center rounded-sm px-2 text-caption font-medium text-warn transition-colors hover:bg-cream-deep"
        >
          Jump to comment
        </a>
      ) : null}
      {expiredByTargetChange ? (
        <div className="flex flex-wrap items-center gap-2">
          <RequestConfirmationTargetChip
            issueId={issueId}
            target={staleTarget}
            tone="subtle"
          />
          {staleTarget && target ? (
            <ChevronRight size={16} className="text-warn" />
          ) : null}
          <RequestConfirmationTargetChip issueId={issueId} target={target} />
        </div>
      ) : null}
    </div>
  );
}

function ConfirmationFailedBlock() {
  return (
    <p className="text-ui leading-6 text-[var(--midnight-muted)]">
      This request could not be resolved. Try again or create a new request.
    </p>
  );
}

function RequestConfirmationResolution({
  interaction,
}: {
  interaction: RequestConfirmationInteraction;
}) {
  const target = interaction.payload.target ?? null;

  if (interaction.status === "accepted") {
    return (
      <div className="flex flex-wrap items-center gap-2 text-ui leading-6 text-[var(--midnight)]">
        <span className="font-medium">Confirmed</span>
        <RequestConfirmationTargetChip issueId={interaction.issueId} target={target} />
      </div>
    );
  }

  if (interaction.status === "rejected") {
    return (
      <ConfirmationDeclinedBlock
        issueId={interaction.issueId}
        result={interaction.result}
        target={target}
      />
    );
  }

  if (interaction.status === "expired") {
    return (
      <ConfirmationExpiredBlock
        issueId={interaction.issueId}
        result={interaction.result}
        target={target}
      />
    );
  }

  if (interaction.status === "failed") {
    return <ConfirmationFailedBlock />;
  }

  return null;
}

function RequestConfirmationCard({
  interaction,
  onAcceptInteraction,
  onRejectInteraction,
}: {
  interaction: RequestConfirmationInteraction;
  onAcceptInteraction?: (
    interaction: RequestConfirmationInteraction,
  ) => Promise<void> | void;
  onRejectInteraction?: (
    interaction: RequestConfirmationInteraction,
    reason?: string,
  ) => Promise<void> | void;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [working, setWorking] = useState<"accept" | "reject" | null>(null);
  const [rejectReason, setRejectReason] = useState(interaction.result?.reason ?? "");
  const [rejectAttempted, setRejectAttempted] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const rejectRequiresReason = interaction.payload.rejectRequiresReason === true;
  const allowDeclineReason = interaction.payload.allowDeclineReason !== false;
  const trimmedRejectReason = rejectReason.trim();
  const canReject = !rejectRequiresReason || trimmedRejectReason.length > 0;
  const declineReasonInvalid = rejectRequiresReason && !canReject;
  const declineReasonPlaceholder =
    interaction.payload.declineReasonPlaceholder
    ?? (interaction.payload.acceptLabel === "Approve plan"
      ? "Optional: what would you like revised?"
      : "Optional: tell the agent what you'd change.");

  useEffect(() => {
    setRejectReason(interaction.result?.reason ?? "");
    setRejectAttempted(false);
    setActionError(null);
    if (interaction.status !== "pending") {
      setRejecting(false);
      setWorking(null);
    }
  }, [interaction.id, interaction.result?.reason, interaction.status]);

  async function handleAccept() {
    if (!onAcceptInteraction) return;
    setWorking("accept");
    setActionError(null);
    try {
      await onAcceptInteraction(interaction);
    } catch {
      setActionError("Try again");
    } finally {
      setWorking(null);
    }
  }

  async function handleReject() {
    setRejectAttempted(true);
    if (!onRejectInteraction || !canReject) return;
    setWorking("reject");
    setActionError(null);
    try {
      await onRejectInteraction(interaction, trimmedRejectReason || undefined);
      setRejecting(false);
    } catch {
      setActionError("Try again");
    } finally {
      setWorking(null);
    }
  }

  return (
    <div className="space-y-4">
      {interaction.status === "pending" ? (
        <div className="space-y-3 rounded-sm border border-[var(--edge)] bg-[var(--cream-deep)] p-4">
          <div className="text-ui leading-6 text-[var(--midnight)]">
            {interaction.payload.prompt}
          </div>
          {interaction.payload.detailsMarkdown ? (
            <div className="border-t border-[var(--edge)] pt-3 text-ui">
              <TaskMarkdown body={interaction.payload.detailsMarkdown} />
            </div>
          ) : null}
          <RequestConfirmationTargetChip
            issueId={interaction.issueId}
            target={interaction.payload.target}
          />
        </div>
      ) : null}

      {interaction.status === "pending" ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-end gap-2">
            <EditorialButton
              type="button"
              size="sm"
              variant={rejecting ? "secondary" : "primary"}
              disabled={!onAcceptInteraction || working !== null}
              onClick={() => void handleAccept()}
            >
              {working === "accept" ? (
                <>
                  <Loader2 size={16} className="mr-2 animate-spin" />
                  Confirming...
                </>
              ) : (
                interaction.payload.acceptLabel ?? "Confirm"
              )}
            </EditorialButton>
            <EditorialButton
              type="button"
              size="sm"
              variant="secondary"
              disabled={!onRejectInteraction || working !== null}
              onClick={() => {
                if (!allowDeclineReason) {
                  void handleReject();
                  return;
                }
                setRejectAttempted(false);
                setRejecting((current) => !current);
              }}
            >
              {interaction.payload.rejectLabel ?? "Decline"}
            </EditorialButton>
          </div>

          {rejecting ? (
            <div className="space-y-3 rounded-sm border border-[var(--edge)] bg-[var(--cream-deep)] p-3">
              <Textarea
                value={rejectReason}
                onChange={(event) => setRejectReason(event.target.value)}
                placeholder={declineReasonPlaceholder}
                aria-invalid={rejectAttempted && declineReasonInvalid}
                className="min-h-24 text-ui"
                style={{
                  background: "var(--cream-deep)",
                  border: `1px solid ${rejectAttempted && declineReasonInvalid ? "var(--err)" : "var(--edge)"}`,
                  color: "var(--midnight)",
                }}
              />
              {rejectAttempted && declineReasonInvalid ? (
                <p className="text-caption text-[var(--err)]">A decline reason is required.</p>
              ) : null}
              <div className="flex flex-wrap justify-end gap-2">
                <EditorialButton
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={working !== null}
                  onClick={() => {
                    setRejecting(false);
                    setRejectAttempted(false);
                  }}
                >
                  Cancel decline
                </EditorialButton>
                <EditorialButton
                  type="button"
                  size="sm"
                  variant="secondary"
                  disabled={!onRejectInteraction || working !== null}
                  onClick={() => void handleReject()}
                >
                  {working === "reject" ? (
                    <>
                      <Loader2 size={16} className="mr-2 animate-spin" />
                      Saving...
                    </>
                  ) : (
                    interaction.payload.rejectLabel ?? "Decline"
                  )}
                </EditorialButton>
              </div>
            </div>
          ) : null}

          {actionError ? (
            <div className="rounded-sm border border-[var(--err)] bg-[var(--cream-deep)] px-3 py-2 text-ui text-[var(--err)]">
              {actionError}
            </div>
          ) : null}
        </div>
      ) : (
        <RequestConfirmationResolution interaction={interaction} />
      )}
    </div>
  );
}

const CHECKBOX_SUMMARY_LABEL_LIMIT = 8;

function RequestCheckboxConfirmationResolution({
  interaction,
}: {
  interaction: RequestCheckboxConfirmationInteraction;
}) {
  const target = interaction.payload.target ?? null;
  const [expanded, setExpanded] = useState(false);

  if (interaction.status === "accepted") {
    const totalOptions = interaction.payload.options.length;
    const selectedLabels = getCheckboxConfirmationSelectedLabels({
      payload: interaction.payload,
      result: interaction.result,
    });
    const selectedCount = interaction.result?.selectedOptionIds?.length ?? selectedLabels.length;
    const visibleLabels = expanded
      ? selectedLabels
      : selectedLabels.slice(0, CHECKBOX_SUMMARY_LABEL_LIMIT);
    const hiddenCount = selectedLabels.length - CHECKBOX_SUMMARY_LABEL_LIMIT;
    const hasHiddenLabels = hiddenCount > 0;
    const chipClassName =
      "inline-flex items-center rounded-sm border border-[var(--edge)] bg-transparent px-2 py-0.5 label-caps text-[var(--midnight-muted)]";

    return (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-ui leading-6 text-[var(--midnight)]">
          <span className="font-medium">
            {selectedCount === 0
              ? "Confirmed with no options selected"
              : `Confirmed ${selectedCount} of ${totalOptions} ${totalOptions === 1 ? "option" : "options"}`}
          </span>
          <RequestConfirmationTargetChip issueId={interaction.issueId} target={target} />
        </div>
        {visibleLabels.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {visibleLabels.map((label, index) => (
              <TaskField key={`${label}-${index}`} label="Selected" value={label} />
            ))}
            {hasHiddenLabels ? (
              <button
                type="button"
                onClick={() => setExpanded((current) => !current)}
                className={cn(
                  chipClassName,
                  "cursor-pointer transition-colors hover:border-[var(--edge)] hover:text-[var(--midnight)]",
                )}
                aria-expanded={expanded}
              >
                {expanded ? "Show less" : `+${hiddenCount} more`}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  }

  if (interaction.status === "rejected") {
    return (
      <ConfirmationDeclinedBlock
        issueId={interaction.issueId}
        result={interaction.result}
        target={target}
      />
    );
  }

  if (interaction.status === "expired") {
    return (
      <ConfirmationExpiredBlock
        issueId={interaction.issueId}
        result={interaction.result}
        target={target}
      />
    );
  }

  if (interaction.status === "failed") {
    return <ConfirmationFailedBlock />;
  }

  return null;
}

function CheckboxOptionRow({
  id,
  label,
  description,
  checked,
  disabled,
  onToggle,
}: {
  id: string;
  label: string;
  description?: string | null;
  checked: boolean;
  disabled: boolean;
  onToggle: (checked: boolean) => void;
}) {
  return (
    <label
      htmlFor={id}
      className={cn(
        "flex cursor-pointer items-start gap-2.5 border-b border-[var(--edge)] px-3 py-2 last:border-b-0 transition-colors",
        checked ? "bg-[var(--cream-deeper)]" : "hover:bg-[var(--cream-deep)]",
        disabled && "cursor-not-allowed opacity-60",
      )}
    >
      <Checkbox
        id={id}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(value) => onToggle(value === true)}
        aria-label={label}
        className="mt-0.5 border-[var(--edge)] data-[state=checked]:border-[var(--midnight)] data-[state=checked]:bg-[var(--midnight)] data-[state=checked]:text-[var(--cream)]"
      />
      <div className="min-w-0 flex-1">
        <div className="text-ui font-medium leading-5 text-[var(--midnight)]">{label}</div>
        {description ? (
          <p className="mt-0.5 text-ui leading-5 text-[var(--midnight-muted)]">{description}</p>
        ) : null}
      </div>
    </label>
  );
}

function RequestCheckboxConfirmationCard({
  interaction,
  onAcceptInteraction,
  onRejectInteraction,
}: {
  interaction: RequestCheckboxConfirmationInteraction;
  onAcceptInteraction?: (
    interaction: RequestCheckboxConfirmationInteraction,
    selectedClientKeys: undefined,
    selectedOptionIds: string[],
  ) => Promise<void> | void;
  onRejectInteraction?: (
    interaction: RequestCheckboxConfirmationInteraction,
    reason?: string,
  ) => Promise<void> | void;
}) {
  const options = interaction.payload.options;
  const optionIds = useMemo(() => options.map((option) => option.id), [options]);
  const validOptionIds = useMemo(() => new Set(optionIds), [optionIds]);
  const minSelected = interaction.payload.minSelected ?? 0;
  const maxSelected = interaction.payload.maxSelected ?? null;

  const defaultSelected = useMemo(
    () =>
      new Set(
        (interaction.payload.defaultSelectedOptionIds ?? []).filter((id) => validOptionIds.has(id)),
      ),
    [interaction.payload.defaultSelectedOptionIds, validOptionIds],
  );

  const [selectedOptionIds, setSelectedOptionIds] = useState<Set<string>>(() => new Set(defaultSelected));
  const [rejecting, setRejecting] = useState(false);
  const [working, setWorking] = useState<"accept" | "reject" | null>(null);
  const [rejectReason, setRejectReason] = useState(interaction.result?.reason ?? "");
  const [rejectAttempted, setRejectAttempted] = useState(false);
  const [acceptAttempted, setAcceptAttempted] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const optionSeed = useMemo(() => optionIds.join("\n"), [optionIds]);

  useEffect(() => {
    setSelectedOptionIds(new Set(defaultSelected));
    setRejectReason(interaction.result?.reason ?? "");
    setRejectAttempted(false);
    setAcceptAttempted(false);
    setActionError(null);
    if (interaction.status !== "pending") {
      setRejecting(false);
      setWorking(null);
    }
  }, [interaction.id, interaction.status, interaction.result?.reason, defaultSelected, optionSeed]);

  const rejectRequiresReason = interaction.payload.rejectRequiresReason === true;
  const allowDeclineReason = interaction.payload.allowDeclineReason !== false;
  const trimmedRejectReason = rejectReason.trim();
  const canReject = !rejectRequiresReason || trimmedRejectReason.length > 0;
  const declineReasonInvalid = rejectRequiresReason && !canReject;
  const declineReasonPlaceholder =
    interaction.payload.declineReasonPlaceholder ?? "Optional: tell the agent what you'd change.";

  const selectedCount = selectedOptionIds.size;
  const totalOptions = options.length;
  const atMax = maxSelected != null && selectedCount >= maxSelected;
  const belowMin = selectedCount < minSelected;
  const aboveMax = maxSelected != null && selectedCount > maxSelected;
  const selectionValid = !belowMin && !aboveMax;

  const validationMessage = belowMin
    ? minSelected === 1
      ? "Select at least 1 option."
      : `Select at least ${minSelected} options.`
    : aboveMax && maxSelected != null
      ? maxSelected === 1
        ? "Select at most 1 option."
        : `Select at most ${maxSelected} options.`
      : null;

  function toggleOption(optionId: string, checked: boolean) {
    setSelectedOptionIds((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(optionId);
      } else {
        next.delete(optionId);
      }
      return next;
    });
  }

  function handleSelectAll() {
    const capped = maxSelected != null ? optionIds.slice(0, maxSelected) : optionIds;
    setSelectedOptionIds(new Set(capped));
  }

  function handleClearSelection() {
    setSelectedOptionIds(new Set());
  }

  async function handleAccept() {
    setAcceptAttempted(true);
    if (!onAcceptInteraction || !selectionValid) return;
    setWorking("accept");
    setActionError(null);
    try {
      await onAcceptInteraction(interaction, undefined, [...selectedOptionIds]);
    } catch {
      setActionError("Try again");
    } finally {
      setWorking(null);
    }
  }

  async function handleReject() {
    setRejectAttempted(true);
    if (!onRejectInteraction || !canReject) return;
    setWorking("reject");
    setActionError(null);
    try {
      await onRejectInteraction(interaction, trimmedRejectReason || undefined);
      setRejecting(false);
    } catch {
      setActionError("Try again");
    } finally {
      setWorking(null);
    }
  }

  if (interaction.status !== "pending") {
    return (
      <div className="space-y-4">
        <RequestCheckboxConfirmationResolution interaction={interaction} />
      </div>
    );
  }

  const selectionSummary = totalOptions > 0 && selectedCount === totalOptions
    ? `All ${totalOptions} options selected`
    : `${selectedCount} of ${totalOptions} ${totalOptions === 1 ? "option" : "options"} selected`;
  const boundsHint = maxSelected != null
    ? `Pick ${minSelected === maxSelected ? `exactly ${maxSelected}` : `${minSelected}-${maxSelected}`}.`
    : minSelected > 0
      ? `Pick at least ${minSelected}.`
      : null;

  return (
    <div className="space-y-4">
      <div className="space-y-3 rounded-sm border border-[var(--edge)] bg-[var(--cream-deep)] p-4">
        <div className="text-ui leading-6 text-[var(--midnight)]">{interaction.payload.prompt}</div>
        {interaction.payload.detailsMarkdown ? (
          <div className="border-t border-[var(--edge)] pt-3 text-ui">
            <TaskMarkdown body={interaction.payload.detailsMarkdown} />
          </div>
        ) : null}
        <RequestConfirmationTargetChip
          issueId={interaction.issueId}
          target={interaction.payload.target}
        />
      </div>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2 text-caption text-[var(--midnight-muted)]">
            <span>{selectionSummary}</span>
            {boundsHint ? <span>{boundsHint}</span> : null}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <EditorialButton
              type="button"
              size="sm"
              variant="ghost"
              disabled={working !== null || selectedCount === totalOptions || (maxSelected != null && selectedCount >= maxSelected)}
              onClick={handleSelectAll}
            >
              Select all
            </EditorialButton>
            <EditorialButton
              type="button"
              size="sm"
              variant="ghost"
              disabled={working !== null || selectedCount === 0}
              onClick={handleClearSelection}
            >
              Clear selection
            </EditorialButton>
          </div>
        </div>

        <div
          role="group"
          aria-label="Selectable options"
          className="max-h-80 overflow-y-auto rounded-sm border border-[var(--edge)]"
        >
          {options.map((option) => {
            const checked = selectedOptionIds.has(option.id);
            return (
              <CheckboxOptionRow
                key={option.id}
                id={`${interaction.id}-${option.id}`}
                label={option.label}
                description={option.description}
                checked={checked}
                disabled={working !== null || (!checked && atMax)}
                onToggle={(value) => toggleOption(option.id, value)}
              />
            );
          })}
        </div>

        {acceptAttempted && validationMessage ? (
          <p className="text-caption text-[var(--err)]">{validationMessage}</p>
        ) : null}

        <div className="flex flex-wrap items-center justify-end gap-2">
          <EditorialButton
            type="button"
            size="sm"
            variant={rejecting ? "secondary" : "primary"}
            disabled={!onAcceptInteraction || working !== null}
            onClick={() => void handleAccept()}
          >
            {working === "accept" ? (
              <>
                <Loader2 size={16} className="mr-2 animate-spin" />
                Confirming...
              </>
            ) : (
              interaction.payload.acceptLabel ?? "Confirm selected"
            )}
          </EditorialButton>
          <EditorialButton
            type="button"
            size="sm"
            variant="secondary"
            disabled={!onRejectInteraction || working !== null}
            onClick={() => {
              if (!allowDeclineReason) {
                void handleReject();
                return;
              }
              setRejectAttempted(false);
              setRejecting((current) => !current);
            }}
          >
            {interaction.payload.rejectLabel ?? "Request changes"}
          </EditorialButton>
        </div>

        {rejecting ? (
          <div className="space-y-3 rounded-sm border border-[var(--edge)] bg-[var(--cream-deep)] p-3">
            <Textarea
              value={rejectReason}
              onChange={(event) => setRejectReason(event.target.value)}
              placeholder={declineReasonPlaceholder}
              aria-invalid={rejectAttempted && declineReasonInvalid}
              className="min-h-24 text-ui"
              style={{
                background: "var(--cream-deep)",
                border: `1px solid ${rejectAttempted && declineReasonInvalid ? "var(--err)" : "var(--edge)"}`,
                color: "var(--midnight)",
              }}
            />
            {rejectAttempted && declineReasonInvalid ? (
              <p className="text-caption text-[var(--err)]">A reason is required.</p>
            ) : null}
            <div className="flex flex-wrap justify-end gap-2">
              <EditorialButton
                type="button"
                size="sm"
                variant="ghost"
                disabled={working !== null}
                onClick={() => {
                  setRejecting(false);
                  setRejectAttempted(false);
                }}
              >
                Cancel
              </EditorialButton>
              <EditorialButton
                type="button"
                size="sm"
                variant="secondary"
                disabled={!onRejectInteraction || working !== null}
                onClick={() => void handleReject()}
              >
                {working === "reject" ? (
                  <>
                    <Loader2 size={16} className="mr-2 animate-spin" />
                    Saving...
                  </>
                ) : (
                  interaction.payload.rejectLabel ?? "Request changes"
                )}
              </EditorialButton>
            </div>
          </div>
        ) : null}

        {actionError ? (
          <div className="rounded-sm border border-[var(--err)] bg-[var(--cream-deep)] px-3 py-2 text-ui text-[var(--err)]">
            {actionError}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function IssueThreadInteractionCard({
  interaction,
  agentMap,
  currentUserId,
  onAcceptInteraction,
  onRejectInteraction,
  onSubmitInteractionAnswers,
  onCancelInteraction,
}: IssueThreadInteractionCardProps) {
  const styles = statusClasses(interaction.status);
  const createdByLabel = resolveActorLabel({
    agentId: interaction.createdByAgentId,
    userId: interaction.createdByUserId,
    email: interaction.createdByEmail,
    clerkUserId: interaction.createdByClerkUserId,
    agentMap,
    currentUserId,
  });
  const resolvedByLabel =
    interaction.resolvedByAgentId || interaction.resolvedByUserId
      ? resolveActorLabel({
          agentId: interaction.resolvedByAgentId,
          userId: interaction.resolvedByUserId,
          email: interaction.resolvedByEmail,
          clerkUserId: interaction.resolvedByClerkUserId,
          agentMap,
          currentUserId,
        })
      : null;

  return (
    <div className={cn("rounded-sm border p-5 shadow-none", styles.shell)}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1 basis-64">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn("inline-flex items-center gap-1 rounded-sm border px-2.5 py-1 label-caps", styles.badge)}>
              <StatusGlyph status={interaction.status} className="h-3.5 w-3.5" />
              {interactionKindLabel(interaction.kind)}
              <span className="text-[var(--midnight-faint)]">/</span>
              {statusLabel(interaction.status)}
            </span>
            {interaction.continuationPolicy === "wake_assignee"
              || interaction.continuationPolicy === "wake_assignee_on_accept" ? (
              <span className="inline-flex items-center gap-1 rounded-sm border border-[var(--edge)] bg-transparent px-2.5 py-1 label-caps text-[var(--midnight-soft)]">
                <ListChecks size={16} />
                {interaction.continuationPolicy === "wake_assignee_on_accept"
                  ? "Wakes on confirm"
                  : "Wakes assignee"}
              </span>
            ) : null}
          </div>

          <div className="mt-3 text-title tabular-nums text-[var(--midnight)]">
            {interaction.title
              ?? (interaction.kind === "suggest_tasks"
                ? "Suggested task tree"
                : interaction.kind === "ask_user_questions"
                  ? interaction.payload.title ?? "Questions for the operator"
                  : interaction.kind === "request_checkbox_confirmation"
                    ? "Checkbox confirmation requested"
                    : interaction.kind === "request_item_verdicts"
                      ? "Review items"
                      : "Confirmation requested")}
          </div>
          {interaction.summary ? (
            <p className="mt-2 max-w-3xl text-ui leading-6 text-[var(--midnight-muted)]">
              {interaction.summary}
            </p>
          ) : null}
        </div>

        {/* Tooltip dropped in the editorial port — created-by / date render bare. */}
        <div className="rounded-sm border border-[var(--edge)] bg-transparent px-3 py-2 text-right text-caption text-[var(--midnight-muted)]">
          <div className="font-medium text-[var(--midnight)]">{formatShortDate(interaction.createdAt)}</div>
          <div>proposed by {createdByLabel}</div>
        </div>
      </div>

      <div className="mt-5">
        {interaction.kind === "suggest_tasks" ? (
          <SuggestTasksCard
            interaction={interaction}
            agentMap={agentMap}
            currentUserId={currentUserId}
            onAcceptInteraction={onAcceptInteraction}
            onRejectInteraction={onRejectInteraction}
          />
        ) : interaction.kind === "ask_user_questions" ? (
          <AskUserQuestionsCard
            interaction={interaction}
            onSubmitInteractionAnswers={onSubmitInteractionAnswers}
            onCancelInteraction={onCancelInteraction}
          />
        ) : interaction.kind === "request_checkbox_confirmation" ? (
          <RequestCheckboxConfirmationCard
            interaction={interaction}
            onAcceptInteraction={onAcceptInteraction}
            onRejectInteraction={onRejectInteraction}
          />
        ) : interaction.kind === "request_item_verdicts" ? (
          // Classic has no per-item verdict form; the task chat's ported
          // ItemVerdictsCard (task-chat/TaskChatCompactInteractionCard) owns it.
          <p className="text-ui leading-6 text-[var(--midnight)]">
            {interaction.payload.prompt}{" "}
            <span className="text-[var(--midnight-muted)]">
              {buildIssueThreadInteractionSummary(interaction)} — open the task to review.
            </span>
          </p>
        ) : (
          <RequestConfirmationCard
            interaction={interaction}
            onAcceptInteraction={onAcceptInteraction}
            onRejectInteraction={onRejectInteraction}
          />
        )}
      </div>

      {resolvedByLabel ? (
        <div className="mt-4 border-t border-[var(--edge)] pt-3 text-caption text-[var(--midnight-muted)]">
          Resolved by <span className="font-medium text-[var(--midnight)]">{resolvedByLabel}</span>
          {interaction.resolvedAt ? ` on ${formatShortDate(interaction.resolvedAt)}` : ""}
        </div>
      ) : null}
    </div>
  );
}
