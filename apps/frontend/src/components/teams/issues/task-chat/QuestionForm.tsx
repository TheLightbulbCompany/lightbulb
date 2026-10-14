// apps/frontend/src/components/teams/issues/task-chat/QuestionForm.tsx

// Ported from upstream Paperclip's QuestionForm
// (paperclip/ui/src/components/task-chat/QuestionForm.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Kept from upstream: one question per page with
// "N of M" pagination portalled into the composer takeover's top row, single
// select auto-advancing, multi select, the "Other" custom answer, 1-9 digit
// shortcuts, Skip on optional questions (drops the answer and moves on, or sends
// on the last page), Cancel, and the send that jumps back to the first
// unanswered required question instead of dropping the send.
//
// Adaptations: the question set is always derived from an ask_user_questions
// payload (our interactions carry no native `payload.questionSet`), so only
// single/multi select exist — upstream's text answer mode and textValidation
// patterns are not ported. TaskChatRichInput -> Textarea (no image upload or
// mentions). No draft persistence (composer-draft is not ported). The
// --motion-question-confirm hold before auto-advance is dropped (upstream's
// reduced-motion path). The option filter for >8 options is dropped.

"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { EditorialButton } from "@/components/teams/shell";
import { cn } from "@/lib/utils";
import {
  TaskChatComposerTakeoverControls,
  useTaskChatComposerTakeoverActions,
} from "./TaskChatComposerTakeoverContext";

/** Upstream PaperclipQuestionSet, narrowed to what an ask_user_questions payload produces. */
export interface QuestionSet {
  title?: string;
  description?: string;
  submitLabel?: string;
  questions: Array<{
    id: string;
    prompt: string;
    header?: string;
    helpText?: string;
    required: boolean;
    answerMode: "single_select" | "multi_select";
    options: Array<{ id: string; label: string; description?: string }>;
    customAnswer?: { enabled: true; label?: string; placeholder?: string };
  }>;
}
type Question = QuestionSet["questions"][number];
export interface QuestionAnswer {
  selectedOptionIds?: string[];
  customText?: string;
}
export type QuestionAnswers = Record<string, QuestionAnswer>;

export interface QuestionFormProps {
  id: string;
  questionSet: QuestionSet;
  initialAnswers?: QuestionAnswers;
  disabled?: boolean;
  onSubmit: (answers: QuestionAnswers) => void | Promise<void>;
  /**
   * Resolves the request itself (a timeline card cancelling the interaction).
   * Inside the composer takeover the form falls back to dismissing the
   * takeover, which returns the plain composer without touching the request.
   */
  onCancel?: () => void | Promise<void>;
}

function answerHasValue(answer: QuestionAnswer | undefined): boolean {
  return Boolean(answer?.customText?.trim() || answer?.selectedOptionIds?.length);
}

function answerError(question: Question, answer: QuestionAnswer | undefined): string | null {
  if (question.required && !answerHasValue(answer)) return "This question is required.";
  if (answer?.customText !== undefined && !answer.customText.trim()) return "Enter a custom answer.";
  return null;
}

function SelectOption({
  id,
  label,
  description,
  selected,
  multiple,
  disabled,
  onClick,
}: {
  id: string;
  label: string;
  description?: string;
  selected: boolean;
  multiple: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      id={id}
      role={multiple ? "checkbox" : "radio"}
      aria-checked={selected}
      disabled={disabled}
      className={cn(
        "flex w-full items-start gap-2 rounded-md px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:opacity-60",
        selected ? "bg-[var(--cream-deep)]" : "hover:bg-[var(--cream-deep)]/60",
      )}
      data-selected={selected ? "true" : "false"}
      onClick={onClick}
    >
      <span
        aria-hidden
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center border",
          multiple ? "rounded-sm" : "rounded-full",
          selected
            ? "border-[var(--midnight)] bg-[var(--midnight)] text-[var(--cream)]"
            : "border-[var(--midnight-faint)]",
        )}
      >
        {selected ? (
          multiple ? <Check className="h-3 w-3" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" />
        ) : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-ui font-medium leading-5 text-[var(--midnight)]">{label}</span>
        {description ? (
          <span className="block text-caption leading-4 text-[var(--midnight-muted)]">{description}</span>
        ) : null}
      </span>
    </button>
  );
}

export function QuestionForm({
  id,
  questionSet,
  initialAnswers,
  disabled = false,
  onSubmit,
  onCancel,
}: QuestionFormProps) {
  const takeoverActions = useTaskChatComposerTakeoverActions();
  const [page, setPage] = useState(0);
  const [answers, setAnswers] = useState<QuestionAnswers>(() => structuredClone(initialAnswers ?? {}));
  const [customActive, setCustomActive] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      Object.entries(initialAnswers ?? {})
        .filter(([, answer]) => Boolean(answer.customText))
        .map(([questionId]) => [questionId, true]),
    ),
  );
  const [working, setWorking] = useState<"submit" | "cancel" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const promptRef = useRef<HTMLParagraphElement>(null);
  const previousPage = useRef(page);

  useEffect(() => {
    if (previousPage.current !== page) {
      promptRef.current?.focus();
      previousPage.current = page;
    }
  }, [page]);

  useEffect(() => {
    setPage((current) => Math.min(current, Math.max(questionSet.questions.length - 1, 0)));
  }, [questionSet.questions.length]);

  const validationErrors = useMemo(
    () =>
      Object.fromEntries(
        questionSet.questions.map((candidate) => [candidate.id, answerError(candidate, answers[candidate.id])]),
      ),
    [answers, questionSet.questions],
  );
  const allValid = Object.values(validationErrors).every((value) => value == null);
  const question = questionSet.questions[page];
  if (!question) {
    return <p className="text-ui text-[var(--midnight-muted)]">No answerable questions were provided.</p>;
  }
  const answer = answers[question.id] ?? {};
  const selected = answer.selectedOptionIds ?? [];
  const multiple = question.answerMode === "multi_select";
  const allowsCustom = question.customAnswer?.enabled === true;
  const isCustomActive = customActive[question.id] === true;
  const isLastPage = page === questionSet.questions.length - 1;
  const busy = disabled || working != null;

  function updateAnswer(next: QuestionAnswer) {
    setAnswers((current) => ({ ...current, [question.id]: next }));
  }

  function toggleOption(optionId: string) {
    if (busy) return;
    const optionIds = multiple
      ? selected.includes(optionId)
        ? selected.filter((candidate) => candidate !== optionId)
        : [...selected, optionId]
      : [optionId];
    setAnswers({
      ...answers,
      [question.id]: { ...answer, selectedOptionIds: optionIds, ...(!multiple ? { customText: undefined } : {}) },
    });
    if (!multiple) {
      setCustomActive((current) => ({ ...current, [question.id]: false }));
      // The last page needs an explicit submit.
      if (!isLastPage) {
        setError(null);
        setPage(page + 1);
      }
    }
  }

  function toggleCustom() {
    const active = !isCustomActive;
    setCustomActive((current) => ({ ...current, [question.id]: active }));
    updateAnswer({
      ...answer,
      ...(!multiple && active ? { selectedOptionIds: [] } : {}),
      ...(active ? { customText: answer.customText ?? "" } : { customText: undefined }),
    });
  }

  async function submit(responseAnswers: QuestionAnswers = answers) {
    if (busy) return;
    const invalidIndex = questionSet.questions.findIndex(
      (candidate) => answerError(candidate, responseAnswers[candidate.id]) != null,
    );
    if (invalidIndex >= 0) {
      // A required answer is missing: the pagination arrows browse without
      // validating. Go back to that question and say so rather than dropping the send.
      setPage(invalidIndex);
      setError(`Question ${invalidIndex + 1} needs an answer before you can send.`);
      return;
    }
    setWorking("submit");
    setError(null);
    try {
      await onSubmit(structuredClone(responseAnswers));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The answers could not be submitted.");
    } finally {
      setWorking(null);
    }
  }

  async function cancel() {
    if (!onCancel || busy) return;
    setWorking("cancel");
    setError(null);
    try {
      await onCancel();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The questions could not be cancelled.");
    } finally {
      setWorking(null);
    }
  }

  // Cancel resolves the request when the host owns that; otherwise it just
  // closes the composer takeover so the user can type freely.
  const cancelAction = onCancel ? () => void cancel() : takeoverActions?.dismiss;

  /** Leaves the current question unanswered and moves on (or sends). */
  function skipQuestion() {
    if (busy) return;
    const { [question.id]: _skipped, ...rest } = answers;
    void _skipped;
    setAnswers(rest);
    setCustomActive((current) => ({ ...current, [question.id]: false }));
    if (isLastPage) void submit(rest);
    else setPage(page + 1);
  }

  function progressOrSubmit() {
    if (!isLastPage) {
      setPage((current) => Math.min(current + 1, questionSet.questions.length - 1));
      return;
    }
    void submit();
  }

  const pagination =
    questionSet.questions.length > 1 ? (
      <nav className="flex shrink-0 items-center gap-1 text-caption text-[var(--midnight-muted)]" aria-label="Question pagination">
        <EditorialButton
          type="button"
          size="icon"
          variant="ghost"
          aria-label="Previous question"
          disabled={busy || page === 0}
          onClick={() => setPage((current) => current - 1)}
        >
          <ChevronLeft aria-hidden className="h-4 w-4" />
        </EditorialButton>
        <span className="min-w-10 text-center tabular-nums">
          {page + 1} of {questionSet.questions.length}
        </span>
        <EditorialButton
          type="button"
          size="icon"
          variant="ghost"
          aria-label="Next question"
          // The arrows browse; they do not validate (see submit).
          disabled={busy || isLastPage}
          onClick={() => setPage((current) => current + 1)}
        >
          <ChevronRight aria-hidden className="h-4 w-4" />
        </EditorialButton>
      </nav>
    ) : null;
  const currentError = validationErrors[question.id];

  return (
    <div
      key={question.id}
      data-testid="question-form"
      onKeyDown={(event) => {
        if (busy || event.repeat || event.metaKey || event.ctrlKey || event.altKey) return;
        const target = event.target as HTMLElement;
        if (target.matches("input, textarea, [contenteditable='true']")) return;
        const optionIndex = Number.parseInt(event.key, 10) - 1;
        const option = question.options[optionIndex];
        if (!option || optionIndex < 0 || optionIndex > 8) return;
        event.preventDefault();
        toggleOption(option.id);
      }}
    >
      {questionSet.description ? (
        <p className="mb-3 text-ui text-[var(--midnight-muted)]">{questionSet.description}</p>
      ) : null}
      {pagination ? (
        takeoverActions ? (
          <TaskChatComposerTakeoverControls>{pagination}</TaskChatComposerTakeoverControls>
        ) : (
          <div className="mb-2 flex justify-end">{pagination}</div>
        )
      ) : null}
      <div>
        {question.header ? (
          <p className="mb-1 text-caption font-medium text-[var(--midnight-muted)]">{question.header}</p>
        ) : null}
        <p
          ref={promptRef}
          tabIndex={-1}
          id={`${id}-${question.id}-prompt`}
          className="text-ui font-medium leading-5 text-[var(--midnight)]"
        >
          {question.prompt}
        </p>
        {question.helpText ? (
          <p className="mt-1 text-caption leading-4 text-[var(--midnight-muted)]">{question.helpText}</p>
        ) : null}
      </div>
      <div
        className="mt-3 grid gap-1.5"
        role={multiple ? "group" : "radiogroup"}
        aria-labelledby={`${id}-${question.id}-prompt`}
      >
        {question.options.map((option) => (
          <SelectOption
            key={option.id}
            id={`${id}-${question.id}-${option.id}`}
            label={option.label}
            description={option.description}
            selected={selected.includes(option.id)}
            multiple={multiple}
            disabled={busy}
            onClick={() => toggleOption(option.id)}
          />
        ))}
        {allowsCustom ? (
          <div className="space-y-1.5">
            <SelectOption
              id={`${id}-${question.id}-custom`}
              label={question.customAnswer?.label ?? "Other"}
              selected={isCustomActive}
              multiple={multiple}
              disabled={busy}
              onClick={toggleCustom}
            />
            {isCustomActive ? (
              <Textarea
                aria-labelledby={`${id}-${question.id}-prompt`}
                data-testid="question-other-answer-composer"
                value={answer.customText ?? ""}
                placeholder={question.customAnswer?.placeholder ?? "Type your answer"}
                disabled={busy}
                autoFocus
                onChange={(event) => updateAnswer({ ...answer, customText: event.target.value })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && currentError == null) {
                    event.preventDefault();
                    progressOrSubmit();
                  }
                }}
                className="min-h-16 resize-y text-ui"
              />
            ) : null}
          </div>
        ) : null}
      </div>
      {currentError && answerHasValue(answer) ? (
        <p className="mt-2 text-caption text-[var(--err)]">{currentError}</p>
      ) : null}
      <div aria-live="assertive">
        {error ? (
          <div className="mt-2 rounded-sm border border-[var(--err)] px-2.5 py-2 text-ui text-[var(--err)]">{error}</div>
        ) : null}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
        {cancelAction ? (
          <EditorialButton type="button" size="sm" variant="ghost" disabled={busy} onClick={cancelAction}>
            {working === "cancel" ? <Loader2 aria-hidden className="h-4 w-4 animate-spin" /> : null} Cancel
          </EditorialButton>
        ) : null}
        {!question.required ? (
          <EditorialButton type="button" size="sm" variant="secondary" disabled={busy} onClick={skipQuestion}>
            Skip
          </EditorialButton>
        ) : null}
        <EditorialButton
          type="button"
          size="sm"
          disabled={busy || (isLastPage ? !allValid : currentError != null)}
          onClick={progressOrSubmit}
        >
          {working === "submit" ? <Loader2 aria-hidden className="h-4 w-4 animate-spin" /> : null}
          {isLastPage ? (questionSet.submitLabel ?? "Submit answers") : "Next"}
        </EditorialButton>
      </div>
    </div>
  );
}
