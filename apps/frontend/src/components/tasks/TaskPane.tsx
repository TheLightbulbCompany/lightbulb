"use client";

// One request, opened from a card, a link or a panel (mockup step 8): header with its controls,
// a Needs-you card only while something is open, the task
// room as read-only progress, the outputs, and folded Details. Section order is the mockup's.
// Steering is the room's own composer ("Replying to: …", taskReply.ts); a one-line note only when that
// composer isn't on screen (opened from Home or Work).

import { useEffect, useRef, useState, type ReactElement, type RefObject } from "react";
import { ArrowLeft } from "lucide-react";
import {
  ApiError, FREE_TEXT_OPTION, answerVerb, askLine, doneByLine, effectLine, effectName, previewRows, specialistLabel, summaryView, useTask, useTaskQuestions,
  type QuestionView, type TaskDetail, type TaskSummary, type TaskTrace,
} from "@lightbulb/shared";
import { SessionChat, type ChatWords } from "@/components/messaging/SessionChat";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { TaskStatus } from "./TaskCard";
import { sendToTask, useMarkTaskPaneOpen, useRoomComposerShown } from "./taskReply";
import { useTaskActions } from "./useTaskActions";

const TRANSCRIPT_WORDS: ChatWords = {
  label: "Request progress", title: "Progress",
  empty: "Lightbulb posts this request's progress here.",
  unavailable: "This request's conversation isn't available.", full: "This conversation is full.", ended: "This request is closed.",
};


const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
const money = (n: number) => `$${n.toFixed(2).replace(/\.00$/, "")}`;

export function TaskPane({ taskId, viewerId, onClose, headingRef, backLabel = "Back to conversation" }: {
  taskId: string; viewerId: string; onClose: () => void; headingRef?: RefObject<HTMLHeadingElement | null>; backLabel?: string;
}): ReactElement {
  const { tasks } = useLiveEvents();
  const { data, error, mutate } = useTask(tasks, taskId);
  // A 403/404 is final ("not available"); anything else (network, 5xx) gets Retry.
  const gone = error instanceof ApiError && (error.status === 403 || error.status === 404);
  const ownHeading = useRef<HTMLHeadingElement>(null);
  const heading = headingRef ?? ownHeading;
  useMarkTaskPaneOpen(taskId);

  // One read mark per open.
  useEffect(() => {
    void tasks.markRead(taskId).catch(() => undefined);
    capture("task_card_opened", { taskId, source: "pane" });
  }, [tasks, taskId]);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, [heading, taskId]);

  return <aside className="room-context" aria-label="Request" data-task-pane={taskId}
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
    <header className="room-context-header">
      <Button variant="ghost" aria-label={backLabel} onClick={onClose}><ArrowLeft aria-hidden="true" />Back</Button>
      <h3 ref={heading} tabIndex={-1} className="min-w-0 truncate font-semibold">{data?.task.title ?? "Request"}</h3>
    </header>
    {!data
      ? !error ? <p role="status" className="p-4 text-ui text-muted-foreground">Loading…</p>
        : gone ? <p role="status" className="p-4 text-ui text-muted-foreground">This request isn&apos;t available to you.</p>
        : <p role="alert" className="p-4 text-ui text-muted-foreground">This request couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button></p>
      : <TaskPaneBody detail={data} viewerId={viewerId} />}
  </aside>;
}

function TaskPaneBody({ detail, viewerId }: { detail: TaskDetail; viewerId: string }): ReactElement {
  const { tasks } = useLiveEvents();
  const { task } = detail;
  // Who may answer, by name, is the viewer's view (Q's route); the detail holds the bare rows.
  const { data: views } = useTaskQuestions(tasks, detail.questions.length ? task.taskId : null);
  const questions = views?.items ?? [];
  const open = questions.find((q) => q.state === "open");
  const answered = questions.filter((q) => q.state === "answered" && q.answeredBy);
  // A question's card is posted into the task room too: shown once (open at the top, answered as a Decision line),
  // so Progress leaves it out; a cancelled one has no other place and stays in Progress.
  const cancelled = new Set(questions.filter((q) => q.state === "cancelled").map((q) => q.id));
  const hideCard = (m: { kind?: string | null; taskRefs?: { qid?: string }[] | null }) =>
    m.kind === "question" && !m.taskRefs?.some((r) => r.qid && cancelled.has(r.qid));
  // The header's reason ("Ready for review", "Needs approval"), else its word, so the card agrees with it.
  const view = summaryView(task, viewerId);
  const word = view.reason ?? view.word;
  // The room's composer steers this request when it is on screen; otherwise a one-line note here.
  const composerShown = useRoomComposerShown(task.originRoomId);
  const effects = open ? detail.effects.filter((e) => e.state !== "proposed") : detail.effects;
  return <div className="room-context-body flex min-h-0 flex-col gap-3">
    <TaskHeader task={task} viewerId={viewerId} />
    {open && <NeedsYouCard key={open.id} task={task} question={open} label={word} />}
    {answered.length > 0 && <ul aria-label="Decisions" className="grid gap-1 px-1 text-ui text-muted-foreground">
      {answered.map((q) => <li key={q.id}>{answerLine(q)}</li>)}
    </ul>}
    <div className="flex min-h-64 flex-1 flex-col">
      <SessionChat sessionId={detail.taskRoomId} sessionOwnerId={task.ownerUserId ?? ""} words={TRANSCRIPT_WORDS}
        openChat={async (roomId) => ({ roomId })} readOnly hideMessage={hideCard} />
    </div>
    {/* One text box: the room's composer when it is on screen, else this line; never beside a typed answer. */}
    {!composerShown && !(open?.canAnswer && !open.options.length) && <AddNote task={task} />}
    {/* With a question open, its card above already shows the output and the pending effect. */}
    {task.output && !open && <section aria-label="Outputs" className="editorial-card px-3 py-2 text-ui">
      <span aria-hidden="true">📎 </span>{task.output.title}
    </section>}
    {effects.length > 0 && <ul aria-label="Effects" className="grid gap-1 px-1 text-ui">
      {effects.map((e) => {
        const line = effectLine(e);
        const warn = e.state === "unknown" || line.startsWith("Didn't happen");
        return <li key={e.idemKey} className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-semibold">{effectName(e)}</span>
          <span className={warn ? "text-warn" : "text-muted-foreground"}>{line}</span>
        </li>;
      })}
    </ul>}
    <TaskDetails detail={detail} />
  </div>;
}

function answerLine(q: QuestionView): string {
  const verb = answerVerb(q.answer?.optionId, q.options.find((o) => o.id === q.answer?.optionId)?.label);
  return `${verb} by ${q.answeredBy!.name} · ${time(q.answeredAt)}`;
}

export function TaskHeader({ task, viewerId }: { task: TaskSummary; viewerId: string }): ReactElement {
  const { act } = useTaskActions("pane");
  const { agentNames } = useLiveEvents();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const view = summaryView(task, viewerId);
  // Stop only while something runs: a request that needs the viewer (or is done) has nothing to stop.
  const live = view.word === "Working";
  // The specialist is the actor: "Growth is working on this" / "Done by Growth"; its bare name where nobody did it.
  const actor = doneByLine(task.specialist, view, agentNames) ?? specialistLabel(task.specialist, agentNames);
  const press = async (verb: "stop" | "resolve" | "reopen") => {
    setConfirmStop(false);
    setPending(true);
    const res = await act(task, verb);
    setPending(false);
    setError(res.ok ? null : res.message);
  };
  return <section aria-label="Request status" className="grid gap-1 px-1">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui text-muted-foreground">
      <span className="font-semibold text-foreground">{actor}</span>
      <TaskStatus task={task} viewerId={viewerId} />
      {/* "Done by X, ready for your review" already says Ready for review. */}
      {view.reason && !(view.reason === "Ready for review" && view.word === "Needs you" && actor.startsWith("Done by")) && <span>{view.reason}</span>}
    </div>
    <div className="flex flex-wrap gap-2">
      {/* A running request is stopped (asked once), never resolved under it; Reopen undoes either. */}
      {live && (confirmStop
        ? <span role="group" aria-label="Stop this request?" className="inline-flex items-center gap-2 text-ui">
          <span>Stop this request?</span>
          <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void press("stop")}>Stop it</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmStop(false)}>Keep going</Button>
        </span>
        : <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => setConfirmStop(true)}>Stop</Button>)}
      {!live && task.status !== "resolved" && <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={() => void press("resolve")}>Close</Button>}
      {task.status === "resolved" && <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void press("reopen")}>Reopen</Button>}
    </div>
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </section>;
}

/** Steering when the room's composer isn't on screen: one line, Enter sends it into the request's room. */
function AddNote({ task }: { task: TaskSummary }): ReactElement {
  const { messaging } = useLiveEvents();
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return <form className="grid gap-1 px-1" onSubmit={(event) => {
    event.preventDefault();
    const note = text.trim();
    if (!note || pending) return;
    setPending(true);
    sendToTask(messaging, task, note, "note").then(() => { setText(""); setError(null); }, () => setError("That didn't go through. Try again.")).finally(() => setPending(false));
  }}>
    <Input aria-label="Add a note" placeholder="Add a note…" value={text} maxLength={4000} disabled={pending}
      onChange={(event) => setText(event.target.value)} />
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </form>;
}

/** The open question. Only an answerer gets buttons; everyone else reads who it waits on. Typed text never answers. */
export function NeedsYouCard({ task, question, label = "Needs you" }: { task: TaskSummary; question: QuestionView; label?: string }): ReactElement {
  const { answer } = useTaskActions("pane");
  const { agentNames } = useLiveEvents();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const [text, setText] = useState("");
  const headingId = `needs-${question.id}`;
  const waiting = question.waitingLabel ?? `Waiting on ${question.answerers.map((a) => a.name).join(" or ")}`;
  const heading = askLine(question, task.specialist, agentNames);
  const rows = previewRows(question.preview, Infinity);
  const send = async (optionId: string, typed?: string) => {
    setPending(true);
    const res = await answer(task, question.id, optionId, { expectVersion: question.version, ...(typed ? { text: typed } : {}) });
    setPending(false);
    if (res.ok) setClosed(true);
    else { setNote(res.message); if (res.answeredBy) setClosed(true); }
  };
  return <section role="group" aria-labelledby={headingId} className="editorial-card border-l-2 border-l-warn px-3 py-2">
    <p className="label-caps">{question.canAnswer ? label : waiting}</p>
    <h4 id={headingId} className="font-semibold">{heading}</h4>
    {question.text && question.text !== heading && <p className="mt-0.5 whitespace-pre-wrap text-ui">{question.text}</p>}
    {rows.length > 0 && <dl aria-label="What this does" className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-border pt-2 text-ui">
      {rows.map((r) => <div key={r.label} className="contents">
        <dt className="text-muted-foreground">{r.label}</dt><dd className="min-w-0 whitespace-pre-wrap break-words">{r.value}</dd>
      </div>)}
    </dl>}
    {question.canAnswer && !closed && (question.options.length
      ? <div className="mt-2 flex flex-wrap gap-2">
        {question.options.map((o) => <Button key={o.id} type="button" size="sm" disabled={pending}
          variant={o.style === "primary" ? "default" : o.style === "danger" ? "outline" : "ghost"}
          onClick={() => void send(o.id)}>{o.label}</Button>)}
      </div>
      // A free-text ask (no options): the answer is what the person types, posted as optionId "answer".
      : <form className="mt-2 grid gap-2" onSubmit={(event) => { event.preventDefault(); if (text.trim()) void send(FREE_TEXT_OPTION, text.trim()); }}>
        <textarea aria-label="Your answer" value={text} onChange={(event) => setText(event.target.value)} rows={3} maxLength={4000}
          className="w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-ui focus-visible:outline-2 focus-visible:outline-ring" />
        <div><Button type="submit" size="sm" disabled={pending || !text.trim()}>Send answer</Button></div>
      </form>)}
    {note && <p role="alert" className="mt-1 text-ui text-err">{note}</p>}
  </section>;
}

export function TaskDetails({ detail }: { detail: TaskDetail }): ReactElement {
  const { tasks } = useLiveEvents();
  const { task, attempts } = detail;
  const [trace, setTrace] = useState<TaskTrace | { error: true } | null | undefined>(undefined);
  const latest = attempts.reduce((n, a) => Math.max(n, a.n), 0);
  const cost = task.budgetUsd != null || task.costUsd
    ? `${money(task.costUsd ?? 0)}${task.budgetUsd != null ? ` of ${money(task.budgetUsd)} budget` : ""}` : null;
  return <details className="px-1 text-ui">
    <summary className="cursor-pointer text-muted-foreground">Details</summary>
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      <dt className="text-muted-foreground">Runs</dt><dd>{attempts.length}</dd>
      {task.runsOn && <><dt className="text-muted-foreground">Runs on</dt><dd>Lightbulb cloud</dd></>}
      {cost && <><dt className="text-muted-foreground">Cost</dt><dd>{cost}</dd></>}
    </dl>
    {latest > 0 && <details className="mt-2" onToggle={(event) => {
      if ((event.currentTarget as HTMLDetailsElement).open && trace === undefined) {
        setTrace(null);
        void tasks.trace(task.taskId, latest).then(setTrace, () => setTrace({ error: true }));
      }
    }}>
      <summary className="cursor-pointer text-muted-foreground">Steps</summary>
      {trace == null ? <p className="text-muted-foreground">Loading…</p>
        : "error" in trace ? <p className="text-muted-foreground">The steps aren&apos;t available.</p>
        : !trace.recentTools.length ? <p className="text-muted-foreground">No steps yet.</p>
        : <ol className="mt-1 max-h-64 overflow-auto text-caption" aria-label={`Run ${trace.attempt} steps`}>
          {trace.recentTools.map((tool, i) => <li key={i} className="truncate">{toolLine(tool)}</li>)}
        </ol>}
    </details>}
  </details>;
}

/** One plain line per bridge tool record ("browser_navigate · done" → "Browser navigate · done"). */
function toolLine(tool: unknown): string {
  const words = (name: string) => { const w = name.replace(/^mcp__[^_]+__/, "").replace(/[_.]+/g, " ").trim(); return w.charAt(0).toUpperCase() + w.slice(1); };
  if (typeof tool === "string") return words(tool);
  if (tool && typeof tool === "object") {
    const t = tool as Record<string, unknown>;
    const name = [t.name, t.tool, t.type].find((v) => typeof v === "string") as string | undefined;
    const status = typeof t.status === "string" ? ` · ${t.status}` : "";
    if (name) return `${words(name)}${status}`;
  }
  return "A step";
}
