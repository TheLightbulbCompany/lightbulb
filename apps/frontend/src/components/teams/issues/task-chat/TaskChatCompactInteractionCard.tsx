// apps/frontend/src/components/teams/issues/task-chat/TaskChatCompactInteractionCard.tsx

// Ported from upstream Paperclip's TaskChatCompactInteractionCard
// (paperclip/ui/src/components/task-chat/TaskChatCompactInteractionCard.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Part B2 subset: the InteractionShell (timeline
// card / takeover header), the resolved-receipt disclosure, the ask_user_questions
// card over the ported QuestionForm, and ItemVerdictsCard (per-item
// approve / reject / defer with required reasons, paginated, the composer's
// Skip beside "Apply N").
//
// Adaptations: brand tokens replace stock colours (approve = --ok, reject =
// --err); EditorialButton replaces shadcn Button; TaskMarkdown replaces
// MarkdownBody. Pending request_confirmation / request_checkbox_confirmation /
// suggest_tasks keep the classic IssueThreadInteractionCard body (stock's
// compact ConfirmationCard / CheckboxConfirmationCard / SuggestedTasksCard and
// the plan-review preview are not ported). No draft persistence
// (composer-draft), no audience line or interaction-resolution-error copy
// (the addressee / resolver-policy fields are not in our types), no
// connection_intent (not in our kinds).

"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  CircleHelp,
  GitBranch,
  ListChecks,
  Loader2,
  MessageSquareQuote,
  X,
} from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { EditorialButton } from "@/components/teams/shell";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, shellUrlFromPaperclipPath } from "@/lib/shellNav";
import type {
  AskUserQuestionsAnswer,
  AskUserQuestionsInteraction,
  IssueThreadInteraction,
  RequestItemVerdictsInteraction,
  RequestItemVerdictValue,
  SubmitIssueThreadInteractionVerdicts,
} from "@/components/teams/shared/types";
import { IssueThreadInteractionCard, type IssueThreadInteractionCardProps } from "../IssueThreadInteractionCard";
import {
  buildIssueThreadInteractionSummary,
  normalizeRequestConfirmationTargetHref,
} from "../lib/issue-thread-interactions";
import { QuestionForm, type QuestionAnswers, type QuestionSet } from "./QuestionForm";
import {
  TaskChatComposerTakeoverControls,
  TaskChatComposerTakeoverHeader,
  useTaskChatComposerTakeoverActions,
} from "./TaskChatComposerTakeoverContext";
import { TaskMarkdown } from "@/components/teams/shared/components/TaskMarkdown";

export type SubmitInteractionVerdicts = (
  interaction: RequestItemVerdictsInteraction,
  verdicts: SubmitIssueThreadInteractionVerdicts["verdicts"],
) => Promise<void> | void;

export interface TaskChatCompactInteractionCardProps extends Omit<IssueThreadInteractionCardProps, "interaction"> {
  interaction: IssueThreadInteraction;
  onSubmitInteractionVerdicts?: SubmitInteractionVerdicts;
  presentation?: "timeline" | "takeover";
}

const KIND_COPY = {
  suggest_tasks: { fallbackTitle: "Suggested tasks", icon: GitBranch },
  ask_user_questions: { fallbackTitle: "Questions", icon: CircleHelp },
  request_confirmation: { fallbackTitle: "Confirmation", icon: CheckCircle2 },
  request_checkbox_confirmation: { fallbackTitle: "Choose options", icon: ListChecks },
  request_item_verdicts: { fallbackTitle: "Review items", icon: MessageSquareQuote },
} as const;

function statusTone(status: IssueThreadInteraction["status"]): string {
  if (status === "accepted" || status === "answered") return "text-[var(--ok)]";
  if (status === "rejected" || status === "failed") return "text-[var(--err)]";
  return "text-[var(--midnight-muted)]";
}

function StatusIcon({ status }: { status: IssueThreadInteraction["status"] }) {
  const className = cn("h-3.5 w-3.5", statusTone(status));
  if (status === "accepted" || status === "answered") return <Check aria-hidden className={className} />;
  if (status === "rejected" || status === "failed") return <X aria-hidden className={className} />;
  return <Circle aria-hidden className={className} />;
}

function InteractionShell({
  interaction,
  presentation,
  children,
}: {
  interaction: IssueThreadInteraction;
  presentation: "timeline" | "takeover";
  children: ReactNode;
}) {
  const kind = KIND_COPY[interaction.kind];
  const Icon = kind.icon;
  const title = (
    <div className="flex min-w-0 items-center gap-2">
      <Icon aria-hidden className="h-4 w-4 shrink-0 text-[var(--midnight-muted)]" />
      <strong className="min-w-0 truncate text-ui font-medium text-[var(--midnight)]">
        {interaction.title ?? kind.fallbackTitle}
      </strong>
      {interaction.status !== "pending" ? (
        <span
          className="inline-flex shrink-0 items-center gap-1 text-caption capitalize text-[var(--midnight-muted)]"
          data-testid="interaction-status-badge"
        >
          <StatusIcon status={interaction.status} />
          {interaction.status.replaceAll("_", " ")}
        </span>
      ) : null}
    </div>
  );
  return (
    <article className={cn("overflow-hidden", presentation === "timeline" && "rounded-md border border-[var(--edge)]")}>
      {presentation === "takeover" ? (
        <TaskChatComposerTakeoverHeader>{title}</TaskChatComposerTakeoverHeader>
      ) : (
        <header className="flex min-w-0 items-start gap-2 px-3 py-2.5">{title}</header>
      )}
      <div className={presentation === "timeline" ? "border-t border-[var(--edge-soft)] px-3 py-3" : undefined}>
        {children}
      </div>
    </article>
  );
}

function Details({ children }: { children?: ReactNode }) {
  if (!children) return null;
  return (
    <details className="mt-2">
      <summary className="flex cursor-pointer list-none items-center gap-1 text-caption font-medium text-[var(--midnight-muted)]">
        <ChevronDown aria-hidden className="h-3.5 w-3.5" /> Details
      </summary>
      <div className="mt-2 rounded-sm bg-[var(--cream-deep)] px-2.5 py-2 text-ui text-[var(--midnight-muted)]">
        {children}
      </div>
    </details>
  );
}

function receiptReason(interaction: IssueThreadInteraction): string | null {
  const result = interaction.result;
  if (!result) return null;
  if ("reason" in result && typeof result.reason === "string") return result.reason;
  if (interaction.kind === "ask_user_questions") return interaction.result?.cancellationReason ?? null;
  if (interaction.kind === "suggest_tasks") return interaction.result?.rejectionReason ?? null;
  return null;
}

function questionSetForInteraction(interaction: AskUserQuestionsInteraction): QuestionSet {
  // Upstream synthesizes a question set from the payload's questions when the
  // payload carries no native questionSet (ours never does).
  return {
    ...(interaction.title ? { title: interaction.title } : {}),
    ...(interaction.payload.submitLabel ? { submitLabel: interaction.payload.submitLabel } : {}),
    questions: interaction.payload.questions.map((question) => ({
      id: question.id,
      prompt: question.prompt,
      ...(question.helpText ? { helpText: question.helpText } : {}),
      required: question.required === true,
      answerMode: question.selectionMode === "multi" ? ("multi_select" as const) : ("single_select" as const),
      options: question.options.map((option) => ({
        id: option.id,
        label: option.label,
        ...(option.description ? { description: option.description } : {}),
      })),
      // Upstream's implicitCustomAnswer: a synthesized set always offers "Other".
      customAnswer: { enabled: true as const },
    })),
  };
}

function answersForInteraction(interaction: AskUserQuestionsInteraction): QuestionAnswers | undefined {
  if (!interaction.result?.answers) return undefined;
  return Object.fromEntries(
    interaction.result.answers.map((answer) => [
      answer.questionId,
      { selectedOptionIds: answer.optionIds, ...(answer.otherText ? { customText: answer.otherText } : {}) },
    ]),
  );
}

function ReceiptDisclosure({ interaction, compact = false }: { interaction: IssueThreadInteraction; compact?: boolean }) {
  const reason = receiptReason(interaction);
  let request: ReactNode;

  if (interaction.kind === "ask_user_questions") {
    const questionSet = questionSetForInteraction(interaction);
    const answers = answersForInteraction(interaction);
    request = (
      <div className="grid gap-3">
        {questionSet.questions.map((question) => {
          const answer = answers?.[question.id];
          const values = [
            ...(answer?.selectedOptionIds ?? []).map(
              (optionId) => question.options.find((option) => option.id === optionId)?.label ?? optionId,
            ),
            answer?.customText,
          ].filter((value): value is string => Boolean(value));
          return (
            <div key={question.id}>
              <p className="text-ui text-[var(--midnight)]">{question.prompt}</p>
              {question.helpText ? (
                <p className="mt-0.5 text-caption text-[var(--midnight-muted)]">{question.helpText}</p>
              ) : null}
              <p className="mt-1 text-ui font-medium text-[var(--midnight)]">
                Answer: {values.length > 0 ? values.join(", ") : "No answer"}
              </p>
            </div>
          );
        })}
      </div>
    );
  } else if (interaction.kind === "request_checkbox_confirmation") {
    const selected = new Set(interaction.result?.selectedOptionIds ?? []);
    const selectedLabels = interaction.payload.options.filter((option) => selected.has(option.id)).map((o) => o.label);
    request = (
      <div>
        <p className="text-ui text-[var(--midnight)]">{interaction.payload.prompt}</p>
        {interaction.status === "accepted" ? (
          <p className="mt-1 text-ui font-medium text-[var(--midnight)]">
            Answer: {selectedLabels.length > 0 ? selectedLabels.join(", ") : "No options selected"}
          </p>
        ) : null}
        {interaction.payload.detailsMarkdown ? (
          <div className="mt-2 text-ui">
            <TaskMarkdown skipHtml body={interaction.payload.detailsMarkdown} />
          </div>
        ) : null}
      </div>
    );
  } else if (interaction.kind === "request_item_verdicts") {
    const verdicts = new Map((interaction.result?.items ?? []).map((item) => [item.id, item]));
    const decidedItems = interaction.payload.items.filter((item) => verdicts.has(item.id));
    request = (
      <div>
        <p className="text-ui text-[var(--midnight)]">{interaction.payload.prompt}</p>
        {decidedItems.length > 0 ? (
          <ul className="mt-2 grid gap-1 text-ui">
            {decidedItems.map((item) => {
              const verdict = verdicts.get(item.id);
              return (
                <li key={item.id}>
                  <span className="font-medium">{item.label}</span> · {verdict?.verdict}
                  {verdict?.reason ? ` — ${verdict.reason}` : ""}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="mt-1 text-ui font-medium text-[var(--midnight)]">No verdicts submitted</p>
        )}
        {interaction.payload.detailsMarkdown ? (
          <div className="mt-2 text-ui">
            <TaskMarkdown skipHtml body={interaction.payload.detailsMarkdown} />
          </div>
        ) : null}
      </div>
    );
  } else if (interaction.kind === "suggest_tasks") {
    const created = new Set(interaction.result?.createdTasks?.map((task) => task.clientKey) ?? []);
    const createdTasks = interaction.payload.tasks.filter((task) => created.has(task.clientKey));
    request =
      createdTasks.length > 0 ? (
        <ul className="grid gap-1 text-ui">
          {createdTasks.map((task) => (
            <li key={task.clientKey} className="font-medium">
              {task.title}
            </li>
          ))}
        </ul>
      ) : interaction.status === "accepted" ? (
        <p className="text-ui font-medium text-[var(--midnight)]">No tasks created</p>
      ) : null;
  } else {
    request = (
      <div className="grid gap-2">
        <p className="text-ui text-[var(--midnight)]">{interaction.payload.prompt}</p>
        {interaction.payload.detailsMarkdown ? (
          <div className="text-ui">
            <TaskMarkdown skipHtml body={interaction.payload.detailsMarkdown} />
          </div>
        ) : null}
      </div>
    );
  }

  const detail = (
    <div className={compact ? "space-y-3 pb-3 pl-7 pr-1" : "mt-2 space-y-3 border-t border-[var(--edge-soft)] pt-3"}>
      {request}
      {reason ? (
        <p className="text-ui text-[var(--midnight-muted)]">
          <span className="font-medium">Reason:</span> {reason}
        </p>
      ) : null}
    </div>
  );

  if (compact) {
    const Icon = KIND_COPY[interaction.kind].icon;
    const answered = interaction.kind === "ask_user_questions" && interaction.status === "answered";
    return (
      <details
        className="group"
        data-testid={answered ? "task-chat-answered-questions-receipt" : "task-chat-interaction-receipt"}
      >
        <summary className="flex min-h-8 cursor-pointer list-none items-center gap-2 rounded-sm px-1 py-1.5 text-ui leading-5 text-[var(--midnight-muted)] transition-colors hover:text-[var(--midnight)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]">
          <span className="flex w-6 shrink-0 items-center justify-center">
            <Icon aria-hidden className="h-3.5 w-3.5 shrink-0" />
          </span>
          <span>{answered ? "Questions answered" : buildIssueThreadInteractionSummary(interaction)}</span>
          <ChevronRight
            aria-hidden
            className="ml-auto h-3 w-3 shrink-0 opacity-0 transition-[opacity,transform] group-hover:opacity-70 group-focus-visible:opacity-70 group-open:rotate-90"
          />
        </summary>
        {detail}
      </details>
    );
  }

  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-caption font-medium text-[var(--midnight-muted)]">
        View original request and resolution
      </summary>
      {detail}
    </details>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "This request could not be resolved.";
}

function AskUserQuestionsCard({
  interaction,
  onSubmitInteractionAnswers,
  onCancelInteraction,
}: {
  interaction: AskUserQuestionsInteraction;
  onSubmitInteractionAnswers?: IssueThreadInteractionCardProps["onSubmitInteractionAnswers"];
  onCancelInteraction?: IssueThreadInteractionCardProps["onCancelInteraction"];
}) {
  const questionSet = questionSetForInteraction(interaction);
  return (
    <QuestionForm
      id={interaction.id}
      questionSet={questionSet}
      initialAnswers={answersForInteraction(interaction)}
      disabled={!onSubmitInteractionAnswers}
      onSubmit={async (response) => {
        const answers: AskUserQuestionsAnswer[] = questionSet.questions.map((question) => {
          const answer = response[question.id];
          const otherText = answer?.customText?.trim();
          return {
            questionId: question.id,
            optionIds: answer?.selectedOptionIds ?? [],
            ...(otherText ? { otherText } : {}),
          };
        });
        try {
          await onSubmitInteractionAnswers?.(interaction, answers);
        } catch (error) {
          throw new Error(errorMessage(error));
        }
      }}
      onCancel={
        onCancelInteraction
          ? async () => {
              try {
                await onCancelInteraction(interaction);
              } catch (error) {
                throw new Error(errorMessage(error));
              }
            }
          : undefined
      }
    />
  );
}

type VerdictDraft = { verdict: RequestItemVerdictValue; reason: string; confirmed?: boolean };

function ItemVerdictsCard({
  interaction,
  onSubmitInteractionVerdicts,
}: {
  interaction: RequestItemVerdictsInteraction;
  onSubmitInteractionVerdicts?: SubmitInteractionVerdicts;
}) {
  const takeoverActions = useTaskChatComposerTakeoverActions();
  const items = interaction.payload.items;
  const resolvedById = useMemo(
    () => new Map((interaction.result?.items ?? []).map((item) => [item.id, item])),
    [interaction.result],
  );
  const firstPendingIndex = Math.max(
    items.findIndex((item) => !resolvedById.has(item.id)),
    0,
  );
  const [page, setPage] = useState(firstPendingIndex);
  const [drafts, setDrafts] = useState<Map<string, VerdictDraft>>(() => new Map());
  const [working, setWorking] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const enabledVerdicts = interaction.payload.verdicts ?? ["approve", "reject"];
  const requireReasonOn = new Set(interaction.payload.requireReasonOn ?? ["reject"]);

  useEffect(() => setPage((current) => Math.min(current, Math.max(items.length - 1, 0))), [items.length]);
  useEffect(() => {
    setDrafts((current) => {
      const next = new Map(current);
      let changed = false;
      for (const id of resolvedById.keys()) {
        if (next.delete(id)) changed = true;
      }
      return changed ? next : current;
    });
    const nextPending = items.findIndex((candidate) => !resolvedById.has(candidate.id));
    if (nextPending >= 0) setPage(nextPending);
  }, [items, resolvedById]);

  const item = items[page];
  if (!item) return <p className="text-ui text-[var(--midnight-muted)]">No review items were provided.</p>;
  const resolved = resolvedById.get(item.id);
  const draft = drafts.get(item.id);
  const rawHref = item.href ? normalizeRequestConfirmationTargetHref(item.href) : null;
  // Paperclip paths (/issues/<id>) have no route here; open them in the shell.
  const shellHref = shellUrlFromPaperclipPath(rawHref);
  const itemHref = shellHref ?? rawHref;
  const rejectionNeedsConfirmation = Boolean(
    !resolved && draft && requireReasonOn.has(draft.verdict) && (!draft.reason.trim() || draft.confirmed === false),
  );

  function advance() {
    setPage((current) => Math.min(current + 1, items.length - 1));
  }

  function setVerdict(verdict: RequestItemVerdictValue) {
    setDrafts((current) => {
      const next = new Map(current);
      const existing = next.get(item.id);
      next.set(item.id, {
        verdict,
        reason: existing?.verdict === verdict ? existing.reason : "",
        confirmed: !requireReasonOn.has(verdict),
      });
      return next;
    });
    if (!requireReasonOn.has(verdict)) advance();
  }

  function setReason(reason: string) {
    setDrafts((current) => {
      const next = new Map(current);
      const existing = next.get(item.id);
      if (existing) next.set(item.id, { ...existing, reason, confirmed: false });
      return next;
    });
  }

  function confirmRejectionReason() {
    if (!draft?.reason.trim()) return;
    setDrafts((current) => {
      const next = new Map(current);
      const existing = next.get(item.id);
      if (existing) next.set(item.id, { ...existing, confirmed: true });
      return next;
    });
    advance();
  }

  async function submit() {
    if (!onSubmitInteractionVerdicts) return;
    const verdicts = [...drafts].map(([id, value]) => ({
      id,
      verdict: value.verdict,
      ...(value.reason.trim() ? { reason: value.reason.trim() } : {}),
    }));
    if (verdicts.length === 0 || verdicts.some((entry) => requireReasonOn.has(entry.verdict) && !entry.reason)) return;
    setWorking(true);
    setActionError(null);
    try {
      await onSubmitInteractionVerdicts(interaction, verdicts);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setWorking(false);
    }
  }

  const allDraftsValid =
    drafts.size > 0 &&
    [...drafts.values()].every(
      (candidate) =>
        !requireReasonOn.has(candidate.verdict) || Boolean(candidate.reason.trim() && candidate.confirmed !== false),
    );

  return (
    <div data-testid="item-verdicts-card">
      <div className="mb-2 flex items-center text-caption text-[var(--midnight-muted)]">
        <span>{drafts.size + resolvedById.size} decided</span>
        <TaskChatComposerTakeoverControls>
          <nav className="flex shrink-0 items-center gap-1 text-caption text-[var(--midnight-muted)]" aria-label="Item pagination">
            <EditorialButton
              type="button"
              size="icon"
              variant="ghost"
              aria-label="Previous item"
              disabled={working || page === 0}
              onClick={() => setPage((current) => current - 1)}
            >
              <ChevronLeft aria-hidden className="h-4 w-4" />
            </EditorialButton>
            <span className="min-w-10 text-center tabular-nums">
              {page + 1} of {items.length}
            </span>
            <EditorialButton
              type="button"
              size="icon"
              variant="ghost"
              aria-label="Next item"
              disabled={working || page === items.length - 1 || rejectionNeedsConfirmation}
              onClick={() => setPage((current) => current + 1)}
            >
              <ChevronRight aria-hidden className="h-4 w-4" />
            </EditorialButton>
          </nav>
        </TaskChatComposerTakeoverControls>
      </div>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-ui font-medium leading-5 text-[var(--midnight)]">{item.label}</p>
          {item.description ? (
            <p className="text-caption leading-4 text-[var(--midnight-muted)]">{item.description}</p>
          ) : null}
        </div>
        {itemHref ? (
          <a
            href={itemHref}
            onClick={shellHref ? (e) => handleShellLinkClick(e, shellHref) : undefined}
            className="text-caption font-medium text-[var(--link)] hover:underline"
          >
            Open
          </a>
        ) : null}
      </div>
      {item.previewMarkdown ? (
        <div className="mt-2 rounded-sm bg-[var(--cream-deep)] px-2.5 py-2 text-caption">
          <TaskMarkdown skipHtml body={item.previewMarkdown} />
        </div>
      ) : null}
      {resolved ? (
        <p className="mt-3 inline-flex rounded-sm border border-[var(--edge)] px-2 py-1 text-caption capitalize text-[var(--midnight-muted)]">
          {resolved.verdict}
          {resolved.reason ? ` · ${resolved.reason}` : ""}
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap gap-1.5" role="group" aria-label={`Verdict for ${item.label}`}>
          {enabledVerdicts.map((verdict) => {
            const pressed = draft?.verdict === verdict;
            return (
              <EditorialButton
                key={verdict}
                type="button"
                size="sm"
                variant="secondary"
                aria-pressed={pressed}
                onClick={() => setVerdict(verdict)}
                className={cn(
                  "min-w-24 justify-center capitalize",
                  verdict === "approve" && (pressed ? "border-[var(--ok)] bg-[var(--ok)]/15 text-[var(--ok)]" : "text-[var(--ok)]"),
                  verdict === "reject" && (pressed ? "border-[var(--err)] bg-[var(--err)]/15 text-[var(--err)]" : "text-[var(--err)]"),
                  verdict === "defer" && pressed && "bg-[var(--cream-deep)]",
                )}
              >
                {verdict === "approve" ? <Check aria-hidden className="h-4 w-4" /> : null}
                {verdict === "reject" ? <X aria-hidden className="h-4 w-4" /> : null}
                {verdict}
              </EditorialButton>
            );
          })}
        </div>
      )}
      {!resolved && draft && requireReasonOn.has(draft.verdict) ? (
        <div className="mt-2 space-y-1.5">
          <label htmlFor={`${interaction.id}-${item.id}-reason`} className="text-caption font-medium text-[var(--midnight)]">
            {interaction.payload.reasonLabel ?? "Reason"}
          </label>
          <Textarea
            id={`${interaction.id}-${item.id}-reason`}
            value={draft.reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Add a short reason"
            className="min-h-16 resize-y text-ui"
            autoFocus
          />
          <div className="flex justify-end">
            <EditorialButton
              type="button"
              size="sm"
              variant="secondary"
              disabled={!draft.reason.trim() || working}
              onClick={confirmRejectionReason}
            >
              Submit rejection reason
            </EditorialButton>
          </div>
        </div>
      ) : null}
      {interaction.payload.detailsMarkdown ? (
        <Details>
          <TaskMarkdown skipHtml body={interaction.payload.detailsMarkdown} />
        </Details>
      ) : null}
      <div aria-live="assertive" data-testid="interaction-action-error">
        {actionError ? (
          <div className="mt-2 rounded-sm border border-[var(--err)] px-2.5 py-2 text-ui text-[var(--err)]">{actionError}</div>
        ) : null}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
        {takeoverActions?.skipButton}
        <EditorialButton
          type="button"
          size="sm"
          disabled={!allDraftsValid || working || !onSubmitInteractionVerdicts}
          onClick={() => void submit()}
        >
          {working ? <Loader2 aria-hidden className="h-4 w-4 animate-spin" /> : null}
          {drafts.size > 0 ? `Apply ${drafts.size}` : "Apply decisions"}
        </EditorialButton>
      </div>
    </div>
  );
}

export function TaskChatCompactInteractionCard({
  interaction,
  presentation = "timeline",
  onSubmitInteractionVerdicts,
  ...classicProps
}: TaskChatCompactInteractionCardProps) {
  if (presentation === "timeline" && interaction.status !== "pending") {
    return <ReceiptDisclosure interaction={interaction} compact />;
  }
  if (
    interaction.status === "pending" &&
    interaction.kind !== "ask_user_questions" &&
    interaction.kind !== "request_item_verdicts"
  ) {
    // Not ported: stock's compact confirmation / checkbox / suggested-tasks cards.
    return <IssueThreadInteractionCard interaction={interaction} {...classicProps} />;
  }
  return (
    <InteractionShell interaction={interaction} presentation={presentation}>
      {interaction.status !== "pending" ? (
        <ReceiptDisclosure interaction={interaction} />
      ) : interaction.kind === "ask_user_questions" ? (
        <AskUserQuestionsCard
          interaction={interaction}
          onSubmitInteractionAnswers={classicProps.onSubmitInteractionAnswers}
          onCancelInteraction={presentation === "timeline" ? classicProps.onCancelInteraction : undefined}
        />
      ) : (
        <ItemVerdictsCard
          interaction={interaction as RequestItemVerdictsInteraction}
          onSubmitInteractionVerdicts={onSubmitInteractionVerdicts}
        />
      )}
    </InteractionShell>
  );
}
