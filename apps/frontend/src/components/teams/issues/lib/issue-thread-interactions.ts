// apps/frontend/src/components/teams/issues/lib/issue-thread-interactions.ts

// Ported VERBATIM (pure, zero-React) from upstream Paperclip's
// ui/src/lib/issue-thread-interactions.ts (MIT, © 2025 Paperclip AI);
// request_item_verdicts + interactionReplacesComposerSkip added from
// v2026.916.1-isol8.2 (task chat part B2). Only the
// type import source is re-pointed from `@paperclipai/shared` to our local
// `shared/types.ts`. Re-exports the interaction types so the card can import
// them from here (matching upstream's import structure).

export type {
  AskUserQuestionsAnswer,
  AskUserQuestionsInteraction,
  AskUserQuestionsPayload,
  AskUserQuestionsQuestion,
  AskUserQuestionsQuestionOption,
  AskUserQuestionsResult,
  IssueThreadInteraction,
  IssueThreadInteractionActorFields,
  IssueThreadInteractionBase,
  IssueThreadInteractionContinuationPolicy,
  IssueThreadInteractionStatus,
  RequestCheckboxConfirmationInteraction,
  RequestCheckboxConfirmationOption,
  RequestCheckboxConfirmationPayload,
  RequestCheckboxConfirmationResult,
  RequestConfirmationInteraction,
  RequestConfirmationIssueDocumentTarget,
  RequestConfirmationPayload,
  RequestConfirmationResult,
  RequestConfirmationTarget,
  RequestItemVerdictsInteraction,
  RequestItemVerdictsItem,
  RequestItemVerdictsPayload,
  RequestItemVerdictsResult,
  RequestItemVerdictsResultItem,
  RequestItemVerdictValue,
  SubmitIssueThreadInteractionVerdicts,
  SuggestedTaskDraft,
  SuggestTasksInteraction,
  SuggestTasksPayload,
  SuggestTasksResult,
  SuggestTasksResultCreatedTask,
} from "@/components/teams/shared/types";
import type {
  AskUserQuestionsAnswer,
  AskUserQuestionsQuestion,
  IssueThreadInteraction,
  RequestCheckboxConfirmationPayload,
  RequestCheckboxConfirmationResult,
  RequestConfirmationTarget,
  RequestItemVerdictsInteraction,
  SuggestedTaskDraft,
} from "@/components/teams/shared/types";

export interface SuggestedTaskTreeNode {
  task: SuggestedTaskDraft;
  children: SuggestedTaskTreeNode[];
}

/**
 * These takeovers already expose a deliberate non-accept path in their form.
 * Showing the composer's generic Skip beside Reject/Revise duplicates that
 * escape hatch and makes the action hierarchy ambiguous.
 */
export function interactionReplacesComposerSkip(
  interaction: IssueThreadInteraction,
): boolean {
  return interaction.kind === "request_confirmation"
    || interaction.kind === "request_checkbox_confirmation"
    || interaction.kind === "suggest_tasks";
}

export function isIssueThreadInteraction(
  value: unknown,
): value is IssueThreadInteraction {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<IssueThreadInteraction>;
  return typeof candidate.id === "string"
    && typeof candidate.companyId === "string"
    && typeof candidate.issueId === "string"
    && (
      candidate.kind === "suggest_tasks"
      || candidate.kind === "ask_user_questions"
      || candidate.kind === "request_confirmation"
      || candidate.kind === "request_checkbox_confirmation"
      || candidate.kind === "request_item_verdicts"
    );
}

export function buildItemVerdictsSummary(
  interaction: RequestItemVerdictsInteraction,
): string {
  // Upstream getItemVerdictProgress, folded in (its only caller here).
  const resolved = new Map(
    (interaction.result?.items ?? []).map((item) => [item.id, item.verdict] as const),
  );
  let approved = 0;
  let rejected = 0;
  let deferred = 0;
  for (const item of interaction.payload.items) {
    const verdict = resolved.get(item.id);
    if (verdict === "approve") approved += 1;
    else if (verdict === "reject") rejected += 1;
    else if (verdict === "defer") deferred += 1;
  }
  const decided = approved + rejected + deferred;
  if (interaction.status === "answered") {
    const parts = [`${decided} decided`];
    if (approved > 0) parts.push(`${approved} approved`);
    if (rejected > 0) parts.push(`${rejected} rejected`);
    if (deferred > 0) parts.push(`${deferred} deferred`);
    return parts.join(" · ");
  }
  if (interaction.status === "expired") {
    const outcome = interaction.result?.outcome;
    if (outcome === "superseded_by_comment") return "Verdicts expired after comment";
    if (outcome === "stale_target") return "Verdicts expired after target changed";
    return "Verdicts expired";
  }
  return `${decided} of ${interaction.payload.items.length} decided`;
}

export function getCheckboxConfirmationSelectedLabels(args: {
  payload: RequestCheckboxConfirmationPayload;
  result?: RequestCheckboxConfirmationResult | null;
}): string[] {
  const { payload, result } = args;
  const selectedIds = result?.selectedOptionIds ?? [];
  const optionLabelById = new Map(
    payload.options.map((option) => [option.id, option.label] as const),
  );
  return selectedIds
    .map((optionId) => optionLabelById.get(optionId))
    .filter((label): label is string => typeof label === "string");
}

export function normalizeRequestConfirmationTargetHref(href: string) {
  const value = href.trim();
  if (value.startsWith("#")) return value;
  if (value.startsWith("/")) return value.startsWith("//") ? null : value;
  return /^https?:\/\//i.test(value) ? value : null;
}

export function getRequestConfirmationTargetHref({
  issueId,
  target,
}: {
  issueId: string;
  target: RequestConfirmationTarget;
}) {
  // A target on the CURRENTLY-viewed issue is an in-page anchor: the Teams
  // shell has no `/issues/:id` route (the legacy URL tree was removed), but the
  // document row now renders `id="document-<key>"`, so a bare hash scrolls to
  // it. This MUST win over the raw-`href` early return below — a same-issue
  // target may also carry an internal href like `/issues/<current>#document-…`,
  // which normalizes to a non-external, non-hash path the chip renders as a
  // dead span. Cross-issue targets keep their raw href / full path (there's no
  // route to open another issue's document in place). This is what makes the
  // confirmation-target chip a live link instead of a dead span.
  if (target.type === "issue_document") {
    const targetIssueId = target.issueId ?? issueId;
    if (targetIssueId === issueId) {
      return `#document-${encodeURIComponent(target.key)}`;
    }
  }
  if (target.href) {
    const safeHref = normalizeRequestConfirmationTargetHref(target.href);
    if (safeHref) return safeHref;
  }
  if (target.type === "issue_document") {
    const targetIssueId = target.issueId ?? issueId;
    return `/issues/${targetIssueId}#document-${encodeURIComponent(target.key)}`;
  }
  return null;
}

export function buildIssueThreadInteractionSummary(
  interaction: IssueThreadInteraction,
) {
  if (interaction.kind === "suggest_tasks") {
    const count = interaction.payload.tasks.length;
    if (interaction.status === "accepted") {
      const createdCount = interaction.result?.createdTasks?.length ?? 0;
      const skippedCount = interaction.result?.skippedClientKeys?.length ?? 0;
      if (skippedCount > 0) {
        return `Accepted ${createdCount} of ${count} tasks`;
      }
      return createdCount === 1 ? "Accepted 1 task" : `Accepted ${createdCount} tasks`;
    }
    if (interaction.status === "rejected") {
      return count === 1 ? "Rejected 1 task" : `Rejected ${count} tasks`;
    }
    return count === 1 ? "Suggested 1 task" : `Suggested ${count} tasks`;
  }

  if (interaction.kind === "request_confirmation") {
    if (interaction.status === "accepted") return "Confirmed request";
    if (interaction.status === "rejected") return "Declined request";
    if (interaction.status === "expired") {
      const outcome = interaction.result?.outcome;
      if (outcome === "superseded_by_comment") return "Confirmation expired after comment";
      if (outcome === "stale_target") return "Confirmation expired after target changed";
      return "Confirmation expired";
    }
    return "Requested confirmation";
  }

  if (interaction.kind === "request_checkbox_confirmation") {
    const optionCount = interaction.payload.options.length;
    if (interaction.status === "accepted") {
      const selectedCount = interaction.result?.selectedOptionIds?.length ?? 0;
      if (selectedCount === 0) return "Confirmed with no options selected";
      return selectedCount === 1
        ? `Confirmed 1 of ${optionCount} options`
        : `Confirmed ${selectedCount} of ${optionCount} options`;
    }
    if (interaction.status === "rejected") return "Declined selection";
    if (interaction.status === "expired") {
      const outcome = interaction.result?.outcome;
      if (outcome === "superseded_by_comment") return "Selection expired after comment";
      if (outcome === "stale_target") return "Selection expired after target changed";
      return "Selection expired";
    }
    return optionCount === 1
      ? "Requested a selection from 1 option"
      : `Requested a selection from ${optionCount} options`;
  }

  if (interaction.kind === "request_item_verdicts") {
    return buildItemVerdictsSummary(interaction);
  }

  const count = interaction.payload.questions.length;
  if (interaction.status === "answered") {
    return count === 1 ? "Answered 1 question" : `Answered ${count} questions`;
  }
  if (interaction.status === "cancelled") {
    return count === 1 ? "Cancelled 1 question" : `Cancelled ${count} questions`;
  }
  return count === 1 ? "Asked 1 question" : `Asked ${count} questions`;
}

export function buildSuggestedTaskTree(
  tasks: readonly SuggestedTaskDraft[],
): SuggestedTaskTreeNode[] {
  const nodes = new Map<string, SuggestedTaskTreeNode>();
  for (const task of tasks) {
    nodes.set(task.clientKey, { task, children: [] });
  }

  const roots: SuggestedTaskTreeNode[] = [];
  for (const task of tasks) {
    const node = nodes.get(task.clientKey);
    if (!node) continue;
    const parentNode = task.parentClientKey ? nodes.get(task.parentClientKey) : null;
    if (parentNode) {
      parentNode.children.push(node);
      continue;
    }
    roots.push(node);
  }

  return roots;
}

export function countSuggestedTaskNodes(node: SuggestedTaskTreeNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countSuggestedTaskNodes(child), 0);
}

export function collectSuggestedTaskClientKeys(node: SuggestedTaskTreeNode): string[] {
  return [
    node.task.clientKey,
    ...node.children.flatMap((child) => collectSuggestedTaskClientKeys(child)),
  ];
}

export function getQuestionAnswerLabels(args: {
  question: AskUserQuestionsQuestion;
  answers: readonly AskUserQuestionsAnswer[];
}) {
  const { question, answers } = args;
  const answer = answers.find((candidate) => candidate.questionId === question.id);
  const selectedIds = answer?.optionIds ?? [];
  const optionLabelById = new Map(
    question.options.map((option) => [option.id, option.label] as const),
  );
  const labels = selectedIds
    .map((optionId) => optionLabelById.get(optionId))
    .filter((label): label is string => typeof label === "string");
  const otherText = answer?.otherText?.trim();
  if (otherText) labels.push(`Other: ${otherText}`);
  return labels;
}
