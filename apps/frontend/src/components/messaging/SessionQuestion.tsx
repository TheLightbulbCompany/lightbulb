"use client";

import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { canControlSession, useSessionQuestion, type LocalSession, type SessionControlClient } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";

/** Answers the exact provider-held request, independently of terminal sharing. */
export function SessionQuestionCard({ session, client, own, ownerName }: {
  session: LocalSession; client: SessionControlClient; own: boolean; ownerName: string;
}) {
  const pending = session.pendingQuestion;
  const { answer, state, locked } = useSessionQuestion(client, session, () => crypto.randomUUID());
  const { userId } = useAuth();
  const [selected, setSelected] = useState(() => new Map<string, string[]>());
  const [custom, setCustom] = useState(() => new Map<string, string>());
  if (!pending || session.status !== "waiting-approval") return null;
  const allowed = canControlSession(session, own, userId) && session.shared !== false && (!session.controlUnavailableReason || ["history-gap", "runtime-unverified"].includes(session.controlUnavailableReason));
  const answers = Object.fromEntries(pending.questions.map(q => [q.question, custom.get(q.question)?.trim() || (selected.get(q.question) ?? []).join(", ")]));
  const tooLong = new TextEncoder().encode(JSON.stringify(answers)).length > 2000;
  const ready = pending.questions.every(q => !!answers[q.question]) && !tooLong;
  return <form className="local-session-waiting text-ui" aria-label="Session question" onSubmit={event => { event.preventDefault(); if (allowed && ready && !locked) void answer(answers); }}>
    <p className="font-semibold">{allowed ? "Claude has a question" : `Waiting for ${ownerName}’s answer`}</p>
    {pending.questions.map((q, qi) => <fieldset key={q.question} disabled={!allowed || locked} className="my-3">
      <legend className="font-medium">{q.question}</legend>
      <div className="local-session-actions">
        {q.options.map(option => <Button key={option.label} type="button" size="sm" variant={(selected.get(q.question) ?? []).includes(option.label) ? "default" : "outline"}
          className="h-auto flex-col items-start whitespace-normal text-left" aria-pressed={(selected.get(q.question) ?? []).includes(option.label)}
          onClick={() => {
            setCustom(values => new Map(values).set(q.question, ""));
            setSelected(values => { const before = values.get(q.question) ?? []; return new Map(values).set(q.question, q.multiSelect
              ? before.includes(option.label) ? before.filter(label => label !== option.label) : [...before, option.label]
              : [option.label]); });
          }}><span>{option.label}</span>{option.description && <span className="text-caption font-normal">{option.description}</span>}</Button>)}
      </div>
      <label className="mt-2 block" htmlFor={`question-${pending.promptId}-${qi}`}>Your answer</label>
      <input id={`question-${pending.promptId}-${qi}`} className="room-input" maxLength={1000} placeholder="Or write your own…" value={custom.get(q.question) ?? ""}
        onChange={event => setCustom(values => new Map(values).set(q.question, event.target.value))} />
    </fieldset>)}
    {tooLong && <p role="alert">Your answer is too long.</p>}
    {allowed && <Button type="submit" size="sm" disabled={!ready || locked}>Send answer</Button>}
    {state?.status === "sending" && <p role="status">Sending…</p>}
    {(state?.status === "pending" || state?.status === "uncertain") && <p role="status">Waiting for Claude to receive your answer…</p>}
    {state?.status === "applied" && <p role="status">Answered</p>}
    {state?.error && <p role="alert">{state.error}</p>}
  </form>;
}
