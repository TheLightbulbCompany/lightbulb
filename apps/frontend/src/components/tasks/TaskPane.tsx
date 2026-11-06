"use client";

// One task, opened from a card, a link or a panel (mockup step 8): header with its controls,
// a Needs-you card only while something is open, the task
// room as read-only progress, the outputs, and folded Details. Section order is the mockup's.
// Steering is the room's own composer ("Replying to: …", taskReply.ts); a one-line note only when that
// composer isn't on screen (opened from Home or Work).

import { useEffect, useRef, useState, type ReactElement, type RefObject } from "react";
import { ArrowLeft } from "lucide-react";
import {
  ApiError, answerVerb, askLine, effectLine, effectName, specialistLabel, summaryView, useTask, useTaskQuestions,
  type QuestionView, type TaskDetail, type TaskEffect, type TaskSummary, type TaskTrace,
} from "@lightbulb/shared";
import { SessionChat, type ChatWords } from "@/components/messaging/SessionChat";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { DecisionCard, endedLabel } from "./DecisionCard";
import { TaskStatus } from "./TaskCard";
import { sendToTask, useMarkTaskPaneOpen, useMarkTaskSteerable, useRoomComposerShown, useShowSteered } from "./taskReply";
import { useTaskActions } from "./useTaskActions";

const TRANSCRIPT_WORDS: ChatWords = {
  label: "Task progress", title: "Progress",
  empty: "Lightbulb posts this task's progress here.",
  unavailable: "This task's progress isn't available.", full: "This task's progress is full.", ended: "This task is closed.",
};


const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
const money = (n: number) => `$${n.toFixed(2).replace(/\.00$/, "")}`;

export function TaskPane({ taskId, viewerId, onClose, headingRef, backLabel = "Back to chat" }: {
  taskId: string; viewerId: string; onClose: () => void; headingRef?: RefObject<HTMLHeadingElement | null>; backLabel?: string;
}): ReactElement {
  const { tasks, agentNames } = useLiveEvents();
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

  // The pane is the specialist's: its name is the title, the task's own title leads the body.
  const who = data ? specialistLabel(data.task.specialist, agentNames) : "Task";
  return <aside className="room-context" aria-label="Task" data-task-pane={taskId}
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
    <header className="room-context-header">
      <Button variant="ghost" aria-label={backLabel} onClick={onClose}><ArrowLeft aria-hidden="true" />Back</Button>
      <h3 ref={heading} tabIndex={-1} className="min-w-0 truncate font-semibold">{who}</h3>
    </header>
    {!data
      ? !error ? <p role="status" className="p-4 text-ui text-muted-foreground">Loading…</p>
        : gone ? <p role="status" className="p-4 text-ui text-muted-foreground">This task isn&apos;t available to you.</p>
        : <p role="alert" className="p-4 text-ui text-muted-foreground">This task couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button></p>
      : <TaskPaneBody detail={data} viewerId={viewerId} />}
  </aside>;
}

function TaskPaneBody({ detail, viewerId }: { detail: TaskDetail; viewerId: string }): ReactElement {
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
  // The room's composer steers this task when it is on screen; otherwise a one-line note here.
  const composerShown = useRoomComposerShown(task.originRoomId);
  const effects = open ? detail.effects.filter((e) => e.state !== "proposed") : detail.effects;
  const closed = view.word === "Closed";
  // Steering is offered while it runs or needs the viewer; a Done task only after Follow up; a Closed one never.
  const [followUp, setFollowUp] = useState(false);
  const steerable = view.word === "Working" || view.word === "Needs you" || (view.word === "Done" && followUp);
  useMarkTaskSteerable(task.taskId, steerable);
  const effectRows = effectFooter(effects, closed);
  return <div className="room-context-body flex min-h-0 flex-col gap-3">
    <TaskHeader task={task} viewerId={viewerId} />
    {open && <DecisionCard key={open.id} task={task} question={open} ended={closed} />}
    {decided.length > 0 && <ul aria-label="Decisions" className="grid gap-1 px-1 text-ui text-muted-foreground">
      {decided.map((d) => <li key={d.id}>{d.line}</li>)}
    </ul>}
    <div className="flex min-h-64 flex-1 flex-col">
      <SessionChat sessionId={detail.taskRoomId} sessionOwnerId={task.ownerUserId ?? ""} words={TRANSCRIPT_WORDS}
        openChat={async (roomId) => ({ roomId })} readOnly hideMessage={hideCard} />
    </div>
    {/* One text box: the room's composer when it is on screen, else this line; never beside a typed answer. */}
    {view.word === "Done" && !followUp && <div className="px-1">
      <Button type="button" size="sm" variant="outline" onClick={() => setFollowUp(true)}>Follow up</Button>
    </div>}
    {steerable && !composerShown && !(open?.canAnswer && !open.options.length) && <AddNote task={task} autoFocus={followUp} />}
    {/* With a question open, its card above already shows the output and the pending effect. */}
    {task.output && !open && <section aria-label="Outputs" className="editorial-card px-3 py-2 text-ui">
      <span aria-hidden="true">📎 </span>{task.output.title}
    </section>}
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

/** The task's title and its one status word, with the reason beside it only when it adds one; the pane's title is
 *  already the specialist, so nothing here repeats who or the word ("Done by X · Done", audit 02 P2). */
export function TaskHeader({ task, viewerId }: { task: TaskSummary; viewerId: string }): ReactElement {
  const { act } = useTaskActions("pane");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const view = summaryView(task, viewerId);
  // Stop only while something runs: a task that needs the viewer (or is done) has nothing to stop.
  const live = view.word === "Working";
  const press = async (verb: "stop" | "resolve" | "reopen") => {
    setConfirmStop(false);
    setPending(true);
    const res = await act(task, verb);
    setPending(false);
    setError(res.ok ? null : res.message);
  };
  return <section aria-label="Task status" className="grid gap-1 px-1">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui text-muted-foreground">
      <span className="min-w-0 font-semibold text-foreground">{task.title}</span>
      <TaskStatus task={task} viewerId={viewerId} />
      {view.reason && <span>{view.reason}</span>}
    </div>
    {/* Fixed slots (UX audit 2, P0): Stop, Close and Reopen never take each other's place, so a press can't land on
        a button that changed under the cursor. A running task is stopped (asked once, on its own row), never
        resolved under it; Reopen undoes either and runs nothing. */}
    <div className="flex gap-2">
      <span data-action-slot="stop" className="w-[4.5rem]">
        {live && <Button type="button" size="sm" variant="outline" disabled={pending || confirmStop} aria-expanded={confirmStop}
          onClick={() => setConfirmStop(true)}>Stop</Button>}
      </span>
      <span data-action-slot="close" className="w-[4.5rem]">
        {!live && task.status !== "resolved" && <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={() => void press("resolve")}>Close</Button>}
      </span>
      <span data-action-slot="reopen" className="w-[5rem]">
        {task.status === "resolved" && <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void press("reopen")}>Reopen</Button>}
      </span>
    </div>
    {live && confirmStop && <span role="group" aria-label="Stop this task?" className="inline-flex items-center gap-2 text-ui">
      <span>Stop this task?</span>
      <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void press("stop")}>Stop it</Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmStop(false)}>Keep going</Button>
    </span>}
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </section>;
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
