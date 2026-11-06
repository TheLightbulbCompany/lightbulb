"use client";

// One task, opened from a card, a link or a panel (mockup step 8): header with its controls,
// a Needs-you card only while something is open, the task
// room as read-only progress (with its lifecycle lines), its steps, the outputs, and folded Details.
// Steering lives here, in the pane's own box (UX slice 3): the room's composer stays the room's.

import { useEffect, useRef, useState, type ReactElement, type RefObject } from "react";
import {
  ApiError, answerVerb, askLine, effectLine, effectName, specialistLabel, stepLines, summaryView, useTask, useTaskQuestions,
  type QuestionView, type TaskDetail, type TaskEffect, type TaskSummary, type TaskTrace,
} from "@lightbulb/shared";
import { SessionChat, type ChatWords } from "@/components/messaging/SessionChat";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PaneHeader } from "@/components/workspace/PaneHeader";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { TaskOutputs } from "@/components/creations/TaskOutputs";
import { DecisionCard, endedLabel } from "./DecisionCard";
import { TaskStatus } from "./TaskCard";
import { sendToTask, useShowSteered } from "./taskReply";
import { useTaskActions } from "./useTaskActions";

const TRANSCRIPT_WORDS: ChatWords = {
  label: "Task progress", title: "Progress",
  empty: "Each step of this task shows here as it happens.",
  unavailable: "This task's progress isn't available.", full: "This task's progress is full.", ended: "This task is closed.",
};


const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
const money = (n: number) => `$${n.toFixed(2).replace(/\.00$/, "")}`;

export function TaskPane({ taskId, viewerId, onClose, onBack, headingRef }: {
  taskId: string; viewerId: string;
  /** ✕: the pane closes. */
  onClose: () => void;
  /** "‹ Tasks": back to the project's Tasks list (absent when the task was opened from Activity or a link). */
  onBack?: () => void;
  headingRef?: RefObject<HTMLHeadingElement | null>;
}): ReactElement {
  const { tasks, agentNames } = useLiveEvents();
  const { data, error, mutate } = useTask(tasks, taskId);
  // A 403/404 is final ("not available"); anything else (network, 5xx) gets Retry.
  const gone = error instanceof ApiError && (error.status === 403 || error.status === 404);
  const ownHeading = useRef<HTMLHeadingElement>(null);
  const heading = headingRef ?? ownHeading;
  // A refused Stop / Close / Reopen / Retry: the buttons sit in the header row, the reason under it.
  const [actionError, setActionError] = useState<string | null>(null);

  // One read mark per open.
  useEffect(() => {
    void tasks.markRead(taskId).catch(() => undefined);
    capture("task_card_opened", { taskId, source: "pane" });
  }, [tasks, taskId]);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, [heading, taskId]);

  // One header row (UX slice 4): "‹ Tasks", the task's title, who and its one status word as muted meta (never cut),
  // then its lifecycle buttons at the right (W11-2). The body never repeats them; while it runs, the live line under
  // the header ticks the seconds.
  const who = data ? specialistLabel(data.task.specialist, agentNames) : null;
  return <aside className="room-context" aria-label="Task" data-task-pane={taskId}
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); (onBack ?? onClose)(); } }}>
    <PaneHeader crumb={onBack ? { label: "Tasks", onBack } : undefined} title={data?.task.title ?? "Task"} headingRef={heading} onClose={onClose}
      meta={data && <span className="inline-flex items-center gap-1.5">{who} · <TaskStatus task={data.task} viewerId={viewerId} /></span>}
      actions={data && <TaskActions task={data.task} viewerId={viewerId} onError={setActionError} />} />
    {!data
      ? !error ? <p role="status" className="p-4 text-ui text-muted-foreground">Loading…</p>
        : gone ? <p role="status" className="p-4 text-ui text-muted-foreground">This task isn&apos;t available to you.</p>
        : <p role="alert" className="p-4 text-ui text-muted-foreground">This task couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button></p>
      : <TaskPaneBody detail={data} viewerId={viewerId} actionError={actionError} />}
  </aside>;
}

function TaskPaneBody({ detail, viewerId, actionError }: { detail: TaskDetail; viewerId: string; actionError: string | null }): ReactElement {
  const { tasks, agentNames } = useLiveEvents();
  const { task } = detail;
  // Who may answer, by name, is the viewer's view (Q's route); the detail holds the bare rows.
  const { data: views } = useTaskQuestions(tasks, detail.questions.length ? task.taskId : null);
  const questions = views?.items ?? [];
  const open = questions.find((q) => q.state === "open");
  // Decisions: what became of each ask (answered by whom, or closed unanswered), one line each (W8-4): a Close
  // superseded by a Reopen's new live ask of the same kind is gone, and the same closed ask asked again reads once.
  const decided = decisionLines(questions, open, (q) => q.state === "cancelled" ? `${endedLabel(q.kind)}: ${askLine(q, task.specialist, agentNames)}` : answerLine(q));
  // A question's card is posted into the task room too: shown once (open at the top, else a Decision line), so
  // Progress leaves it out, as it does a card for this very task (a Closed one read as an empty card, W7-4).
  const hideCard = (m: { kind?: string | null; taskRefs?: { taskId: string }[] | null }) =>
    m.kind === "question" || (!!m.taskRefs?.length && m.taskRefs.every((r) => r.taskId === task.taskId));
  const view = summaryView(task, viewerId);
  const effects = open ? detail.effects.filter((e) => e.state !== "proposed") : detail.effects;
  const closed = view.word === "Closed";
  // Steering is offered while it runs or needs the viewer; a Done task only after Follow up; a Closed one never.
  const [followUp, setFollowUp] = useState(false);
  const steerable = view.word === "Working" || view.word === "Needs you" || (view.word === "Done" && followUp);
  const effectRows = effectFooter(effects, closed);
  return <div className="room-context-body flex min-h-0 flex-col gap-3">
    <TaskHeader task={task} viewerId={viewerId} error={actionError} />
    {open && <DecisionCard key={open.id} task={task} question={open} ended={closed} />}
    {decided.length > 0 && <ul aria-label="Decisions" className="grid gap-1 px-1 text-ui text-muted-foreground">
      {decided.map((d) => <li key={d.id}>{d.line}</li>)}
    </ul>}
    <div className="flex min-h-64 flex-1 flex-col">
      <SessionChat sessionId={detail.taskRoomId} sessionOwnerId={task.ownerUserId ?? ""} words={TRANSCRIPT_WORDS}
        openChat={async (roomId) => ({ roomId })} readOnly hideMessage={hideCard} />
    </div>
    <TaskSteps detail={detail} live={view.word === "Working"} />
    {/* The pane's own steer box; never beside a typed answer. */}
    {view.word === "Done" && !followUp && <div className="px-1">
      <Button type="button" size="sm" variant="outline" onClick={() => setFollowUp(true)}>Follow up</Button>
    </div>}
    {steerable && !(open?.canAnswer && !open.options.length) && <AddNote task={task} autoFocus={followUp} />}
    {/* What it made, one chip each (a draft waiting on review shows in its card above). */}
    {!!task.outputs?.length && <section aria-label="Outputs" className="px-1"><TaskOutputs task={task} /></section>}
    {effectRows.length > 0 && <ul aria-label="Effects" className="grid gap-1 px-1 text-ui">
      {effectRows.map(({ key, name, line, warn }) => <li key={key} className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-semibold">{name}</span>
        <span className={warn ? "text-warn" : "text-muted-foreground"}>{line}</span>
      </li>)}
    </ul>}
    <TaskDetails detail={detail} />
  </div>;
}

/** One line per decision, in order: a closed ask is dropped once a new ask of its kind is live (Reopen asked again),
 *  and identical lines (the same purchase closed twice) read once, at the latest. */
export function decisionLines(questions: readonly QuestionView[], open: QuestionView | undefined, lineOf: (q: QuestionView) => string): { id: string; line: string }[] {
  const decided = questions.filter((q) => (q.state === "answered" && q.answeredBy) || (q.state === "cancelled" && q.kind !== open?.kind));
  const lines = decided.map((q) => ({ id: q.id, line: lineOf(q) }));
  return lines.filter((d, i) => !lines.slice(i + 1).some((later) => later.line === d.line));
}

/** The effects footer: one row per effect, except that the proposals a Close left unapproved (each Reopen proposes
 *  again) read once: "Purchase · Not approved", not one row per attempt. */
export function effectFooter(effects: readonly TaskEffect[], closed: boolean): { key: string; name: string; line: string; warn: boolean }[] {
  const rows = effects.map((e) => ({
    key: e.idemKey, name: effectName(e),
    // An approval nobody gave before Close: it won't happen, so it no longer waits (W7-4).
    line: closed && e.state === "proposed" ? "Not approved" : effectLine(e),
    warn: e.state === "unknown",
    notApproved: closed && e.state === "proposed",
  }));
  return rows.filter((r, i) => !r.notApproved || !rows.slice(i + 1).some((later) => later.notApproved && later.name === r.name))
    .map(({ key, name, line, warn }) => ({ key, name, line, warn: warn || line.startsWith("Didn't happen") }));
}


function answerLine(q: QuestionView): string {
  const verb = answerVerb(q.answer?.optionId, q.options.find((o) => o.id === q.answer?.optionId)?.label);
  return `${verb} by ${q.answeredBy!.name} · ${time(q.answeredAt)}`;
}

/** Under the header: the reason only when it adds one, and a refused action. The header already carries the title,
 *  who and the word, so nothing here repeats them ("Done by X · Done", audit 02 P2; "Growth · working", W12-17). */
export function TaskHeader({ task, viewerId, error }: { task: TaskSummary; viewerId: string; error: string | null }): ReactElement {
  const view = summaryView(task, viewerId);
  return <section aria-label="Task status" className="grid gap-1 px-1 empty:hidden">
    {view.reason && <p className="text-ui text-muted-foreground">{view.reason}</p>}
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </section>;
}

/** The task's lifecycle buttons, in the pane header's row at the right (W11-2). */
function TaskActions({ task, viewerId, onError }: { task: TaskSummary; viewerId: string; onError: (message: string | null) => void }): ReactElement {
  const { act } = useTaskActions("pane");
  const [pending, setPending] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const view = summaryView(task, viewerId);
  // Stop only while something runs: a task that needs the viewer (or is done) has nothing to stop.
  const live = view.word === "Working";
  // After the status word changes, every button is inert for 600 ms: a press meant for Stop can't land on what
  // took its place (UX audit 2).
  const [shownWord, setShownWord] = useState(view.word);
  const [settling, setSettling] = useState(false);
  if (shownWord !== view.word) { setShownWord(view.word); setSettling(true); }
  useEffect(() => {
    if (!settling) return;
    const timer = window.setTimeout(() => setSettling(false), 600);
    return () => window.clearTimeout(timer);
  }, [settling]);
  const busy = pending || settling;
  const press = async (verb: "stop" | "resolve" | "reopen" | "retry") => {
    setConfirmStop(false);
    setPending(true);
    const res = await act(task, verb);
    setPending(false);
    onError(res.ok ? null : res.message);
  };
  // Stop is asked once, in place of the buttons; a running task is stopped, never resolved under it.
  if (live && confirmStop) return <span role="group" aria-label="Stop this task?" className="inline-flex items-center gap-2 text-ui">
    <span className="whitespace-nowrap">Stop this task?</span>
    <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void press("stop")}>Stop it</Button>
    <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmStop(false)}>Keep going</Button>
  </span>;
  // Stop, Retry, Close and Reopen each keep their own slot (UX audit 2, P0); an empty slot takes no room. Reopen
  // undoes a Stop or a Close and runs nothing.
  return <span className="flex items-center gap-1">
    <span data-action-slot="stop" className="empty:hidden">
      {live && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setConfirmStop(true)}>Stop</Button>}
    </span>
    {/* A failed task runs again from here (the owner's; the server's view offers it). */}
    <span data-action-slot="retry" className="empty:hidden">
      {view.action === "retry" && <Button type="button" size="sm" disabled={busy} onClick={() => void press("retry")}>Retry</Button>}
    </span>
    <span data-action-slot="close" className="empty:hidden">
      {!live && task.status !== "resolved" && <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void press("resolve")}>Close</Button>}
    </span>
    <span data-action-slot="reopen" className="empty:hidden">
      {task.status === "resolved" && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void press("reopen")}>Reopen</Button>}
    </span>
  </span>;
}

/** Steering when the room's composer isn't on screen: one line, Enter sends it into the task's room. */
function AddNote({ task, autoFocus = false }: { task: TaskSummary; autoFocus?: boolean }): ReactElement {
  const { messaging, tasks } = useLiveEvents();
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const showSteered = useShowSteered(tasks);
  return <form className="grid gap-1 px-1" onSubmit={(event) => {
    event.preventDefault();
    const note = text.trim();
    if (!note || pending) return;
    setPending(true);
    sendToTask(messaging, task, note, "note").then(() => { setText(""); setError(null); showSteered(task); }, () => setError("That didn't go through. Try again.")).finally(() => setPending(false));
  }}>
    <Input aria-label="Steer this task" placeholder="Steer this task…" autoFocus={autoFocus} value={text} maxLength={4000} disabled={pending}
      onChange={(event) => setText(event.target.value)} />
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </form>;
}

export function TaskDetails({ detail }: { detail: TaskDetail }): ReactElement {
  const { task, attempts } = detail;
  const cost = task.budgetUsd != null || task.costUsd
    ? `${money(task.costUsd ?? 0)}${task.budgetUsd != null ? ` of ${money(task.budgetUsd)} budget` : ""}` : null;
  return <details className="px-1 text-ui">
    <summary className="cursor-pointer text-muted-foreground">Details</summary>
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      <dt className="text-muted-foreground">Runs</dt><dd>{attempts.length}</dd>
      {task.runsOn && <><dt className="text-muted-foreground">Runs on</dt><dd>Lightbulb cloud</dd></>}
      {cost && <><dt className="text-muted-foreground">Cost</dt><dd>{cost}</dd></>}
    </dl>
  </details>;
}

/** The latest run's steps, under Progress, counted in the heading ("Steps · 2"), in plain words (W12-7). */
function TaskSteps({ detail, live }: { detail: TaskDetail; live: boolean }): ReactElement | null {
  const { tasks } = useLiveEvents();
  const latest = detail.attempts.reduce((n, a) => Math.max(n, a.n), 0);
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<TaskTrace | { error: true } | null>(null);
  const taskId = detail.task.taskId;
  // Read once up front, so the heading can count the steps; again every few seconds while it runs and is open.
  useEffect(() => {
    if (!latest) return;
    let alive = true;
    const read = () => void tasks.trace(taskId, latest).then((t) => { if (alive) setTrace(t); }, () => { if (alive) setTrace({ error: true }); });
    read();
    const timer = live && open ? window.setInterval(read, 5000) : undefined;
    return () => { alive = false; window.clearInterval(timer); };
  }, [open, live, latest, taskId, tasks]);
  if (!latest) return null;
  const lines = trace && !("error" in trace) ? stepLines(trace.recentTools) : null;
  return <details className="px-1 text-ui" onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}>
    <summary className="cursor-pointer text-muted-foreground">
      Steps{lines?.length ? ` · ${lines.length}` : ""}{detail.attempts.length > 1 ? ` · run ${latest}` : ""}
    </summary>
    {trace == null ? <p className="text-muted-foreground">Loading…</p>
      : !lines ? <p className="text-muted-foreground">The steps aren&apos;t available.</p>
      : !lines.length ? <p className="text-muted-foreground">No steps yet.</p>
      : <ol className="mt-1 max-h-64 overflow-auto text-caption" aria-label={`Run ${latest} steps`}>
        {lines.map((line, i) => <li key={i} className="truncate">{line}</li>)}
      </ol>}
  </details>;
}
