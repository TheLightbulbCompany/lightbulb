// frontend/src/components/chat/QuestionCards.tsx
/**
 * Inline structured-question cards, stacked above ChatInput next to the
 * exec-approval cards.
 *
 * The agent's run is BLOCKED in the `ask_user` tool while a card is up (up to
 * 900s, then it gives up and guesses), so the card is deliberately loud and
 * the fastest possible answer is one click: a lone single-select question
 * submits on the option press. Everything else (multi-select, several
 * questions, free text, secrets) collects and submits with Send.
 *
 * Behaviour ported from upstream ui/src/pages/chat/components/chat-question-card.ts;
 * the markup is ours. Answer values are `[...selectedLabels, freeText?]` — the
 * "Other" text is appended raw, with no marker prefix.
 *
 * TODO(paperclip-questions): questions raised by a Paperclip-session run
 * (sessionKey `agent:<id>:paperclip[...]`) are filtered out in useQuestions and
 * currently reach nobody. They belong in the Teams inbox — the hook-up point is
 * a new panel under components/teams/panels/ routed from TeamsPanelRouter's
 * PANEL_LOADERS, fed by an owner-scoped question feed rather than this
 * per-member gateway subscription.
 */

"use client";

import * as React from "react";
import { useEffect, useState } from "react";
import type { Question, QuestionRecord } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useQuestionsContext, type QuestionAnswerValues } from "@/hooks/useQuestions";
import { notifyApiError } from "@/lib/notify";

/** mm:ss remaining, clamped at zero. Display only — the gateway owns expiry
 *  and emits `question.resolved {status:"expired"}` itself, so a browser clock
 *  running ahead of the container can never disable a still-live card. */
function useCountdown(expiresAtMs: number): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const remaining = Math.max(0, expiresAtMs - now);
  const total = Math.floor(remaining / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function QuestionCards() {
  const { questions } = useQuestionsContext();
  if (questions.length === 0) return null;
  return (
    <div className="px-4 pt-2 pb-1 flex flex-col gap-2 max-h-[40vh] overflow-y-auto">
      {questions.map((record) => (
        <QuestionCard key={record.id} record={record} />
      ))}
    </div>
  );
}

function QuestionCard({ record }: { record: QuestionRecord }) {
  const { answer, skip } = useQuestionsContext();
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [freeText, setFreeText] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const countdown = useCountdown(record.expiresAtMs);

  // A store-bound secret is always the only question in its request (the
  // gateway rejects any other shape), so the binding is the card's.
  const secretStore = record.questions[0]?.secretStore;

  const values = (q: Question): string[] => {
    const picked = selected[q.questionId] ?? [];
    const raw = freeText[q.questionId] ?? "";
    // Never trim a secret — leading/trailing bytes can be part of the value.
    const text = q.isSecret ? raw : raw.trim();
    return text ? [...picked, text] : picked;
  };

  const answers: QuestionAnswerValues = Object.fromEntries(
    record.questions.map((q) => [q.questionId, values(q)]),
  );
  const complete = record.questions.every((q) => answers[q.questionId].length > 0);

  /** One press is enough when there is a single question, one choice to make,
   *  and nothing to type. */
  const autoSubmits =
    record.questions.length === 1 &&
    !record.questions[0].multiSelect &&
    !record.questions[0].isOther &&
    !record.questions[0].isSecret &&
    record.questions[0].options.length > 0;

  const submit = async (values: QuestionAnswerValues) => {
    setBusy(true);
    setFailed(false);
    try {
      await answer(record.id, values, secretStore?.allowedHosts);
    } catch (e) {
      notifyApiError(e, "Couldn't send your answer");
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  const onSkip = async () => {
    setBusy(true);
    setFailed(false);
    try {
      await skip(record.id);
    } catch (e) {
      notifyApiError(e, "Couldn't skip");
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (q: Question, label: string) => {
    const current = selected[q.questionId] ?? [];
    const next = q.multiSelect
      ? current.includes(label)
        ? current.filter((v) => v !== label)
        : [...current, label]
      : // Single-select re-press keeps the choice — radios never deselect.
        [label];
    setSelected((prev) => ({ ...prev, [q.questionId]: next }));
    if (autoSubmits) void submit({ [q.questionId]: next });
  };

  return (
    <div
      role="group"
      aria-label={`Agent question: ${record.questions[0]?.header ?? ""}`}
      className="rounded-md border border-[var(--bulb)] bg-[var(--cream)] p-3 text-sm transition-[opacity,translate] duration-(--dur-base) ease-out starting:translate-y-1 starting:opacity-0 motion-reduce:transition-[opacity]"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center px-2 py-0.5 text-xs rounded-sm bg-[var(--bulb)] text-[var(--midnight)] shrink-0">
          {record.questions[0]?.header}
        </span>
        <span
          className="text-xs text-[var(--midnight-muted)] tabular-nums"
          aria-label="Time left to answer"
        >
          {countdown}
        </span>
      </div>

      {record.questions.map((q) => (
        <QuestionBody
          key={q.questionId}
          question={q}
          selected={selected[q.questionId] ?? []}
          freeText={freeText[q.questionId] ?? ""}
          disabled={busy}
          onToggle={(label) => toggle(q, label)}
          onFreeText={(v) => setFreeText((prev) => ({ ...prev, [q.questionId]: v }))}
        />
      ))}

      {secretStore && (
        <div className="mt-2 rounded-sm bg-[var(--cream-deep)] p-2 text-xs text-[var(--midnight-muted)] space-y-1">
          <div>
            Stored on your container as{" "}
            <span className="font-mono text-[var(--midnight)]">{secretStore.name}</span> — never
            shown back to the agent.
          </div>
          {secretStore.reason && <div>{secretStore.reason}</div>}
          {secretStore.allowedHosts && secretStore.allowedHosts.length > 0 && (
            <div>Usable only against {secretStore.allowedHosts.join(", ")}.</div>
          )}
          {record.questions[0]?.secretStoreExisting && (
            <div className="text-[var(--warn)]">This replaces the value already stored.</div>
          )}
        </div>
      )}

      <div className="mt-3 flex gap-2 flex-wrap items-center">
        {!autoSubmits && (
          <Button
            size="sm"
            variant="default"
            disabled={!complete || busy}
            aria-busy={busy}
            onClick={() => void submit(answers)}
          >
            Send
          </Button>
        )}
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void onSkip()}>
          Skip
        </Button>
        {failed && (
          <span className="text-xs text-[var(--err)]">That didn&apos;t send — try again.</span>
        )}
      </div>
    </div>
  );
}

function QuestionBody({
  question,
  selected,
  freeText,
  disabled,
  onToggle,
  onFreeText,
}: {
  question: Question;
  selected: string[];
  freeText: string;
  disabled: boolean;
  onToggle: (label: string) => void;
  onFreeText: (value: string) => void;
}) {
  const textId = `q-free-${question.questionId}`;
  const labelId = `q-label-${question.questionId}`;
  return (
    <div className="mt-2">
      <p id={labelId} className="text-[var(--midnight)]">
        {question.question}
      </p>

      {question.options.length > 0 && (
        <div
          className="mt-2 flex flex-col gap-1.5"
          role={question.multiSelect ? "group" : "radiogroup"}
          aria-labelledby={labelId}
        >
          {question.options.map((option) => {
            const isSelected = selected.includes(option.label);
            return (
              <button
                key={option.label}
                type="button"
                role={question.multiSelect ? "checkbox" : "radio"}
                aria-checked={isSelected}
                disabled={disabled}
                onClick={() => onToggle(option.label)}
                className={`text-left rounded-md border px-3 py-2 transition-colors duration-(--dur-fast) disabled:opacity-60 ${
                  isSelected
                    ? "border-[var(--bulb)] bg-[var(--bulb)] text-[var(--midnight)]"
                    : "border-[var(--edge)] bg-[var(--cream-deep)] text-[var(--midnight)] hover:border-[var(--bulb)]"
                }`}
              >
                <span className="block">{option.label}</span>
                {option.description && (
                  <span className="block text-xs text-[var(--midnight-muted)]">
                    {option.description}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {(question.isOther || question.options.length === 0) && (
        <div className="mt-2">
          {/* With options this field is the "Other" escape hatch and needs its
              own label; with none it IS the answer, so the question text
              labels it — by reference, not a duplicated string. */}
          {question.options.length > 0 && (
            <label htmlFor={textId} className="sr-only">
              Other
            </label>
          )}
          <Input
            id={textId}
            {...(question.options.length === 0 ? { "aria-labelledby": labelId } : {})}
            type={question.isSecret ? "password" : "text"}
            autoComplete={question.isSecret ? "off" : undefined}
            value={freeText}
            disabled={disabled}
            placeholder={question.isSecret ? "Paste the value" : "Type an answer"}
            onChange={(e) => onFreeText(e.target.value)}
          />
        </div>
      )}
    </div>
  );
}
