"use client";

// One task, opened from a card, a link or a panel (mockup step 8): header with its controls,
// a Needs-you card only while something is open, the task
// room as read-only progress (with its lifecycle lines), its steps, the outputs, and folded Details.
// Steering lives here, in the pane's own box (UX slice 3): the room's composer stays the room's.

import { useEffect, useRef, useState, type ReactElement, type ReactNode, type RefObject } from "react";
import { ChevronRight } from "lucide-react";
import {
  ApiError, answerVerb, askLine, effectLine, effectName, specialistLabel, stepLines, stepNow, summaryView, timeOfDay, useTask, useTaskQuestions,
  type QuestionView, type TaskDetail, type TaskEffect, type TaskSummary, type TaskTrace,
} from "@lightbulb/shared";
import { SessionChat, type ChatWords } from "@/components/messaging/SessionChat";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PaneHeader } from "@/components/workspace/PaneHeader";
import { BrowserFrameView } from "@/components/workspace/panels/BrowserFrameView";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { TaskOutputs } from "@/components/creations/TaskOutputs";
import { OutputBackContext } from "@/components/creations/openOutput";
import { DecisionCard, endedLabel } from "./DecisionCard";
import { TaskStatus } from "./TaskCard";
import { sendToTask, useShowSteered } from "./taskReply";
import { useTaskActions } from "./useTaskActions";

const TRANSCRIPT_WORDS: ChatWords = {
  label: "Thread result", title: "Result",
  empty: "Each step of this thread shows here as it happens.",
  unavailable: "This thread's progress isn't available.", full: "This thread's progress is full.", ended: "This thread is closed.",
};


/** "7:20 AM", as chat and Scheduled say it (W14; "07:20 AM" before). */
const time = (iso: string | null) => (iso && !Number.isNaN(Date.parse(iso)) ? timeOfDay(Date.parse(iso)) : "");
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
  return <aside className="room-context" aria-label="Thread" data-task-pane={taskId}
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); (onBack ?? onClose)(); } }}>
    <PaneHeader crumb={onBack ? { label: "Threads", onBack } : undefined} title={data?.task.title ?? "Thread"} headingRef={heading} onClose={onClose}
      meta={data && <span className="inline-flex items-center gap-1.5">{who} · <TaskStatus task={data.task} viewerId={viewerId} />
        {summaryView(data.task, viewerId).word === "Working" && <> · <Ticking since={data.task.activity?.startedAt ?? data.task.updatedAt} /></>}</span>}
      actions={data && <TaskActions task={data.task} viewerId={viewerId} onError={setActionError} />} />
    {!data
      ? !error ? <p role="status" className="p-4 text-ui text-muted-foreground">Loading…</p>
        : gone ? <p role="status" className="p-4 text-ui text-muted-foreground">This thread isn&apos;t available to you.</p>
        : <p role="alert" className="p-4 text-ui text-muted-foreground">This thread couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button></p>
      // A doc opened from this task's Progress comes back here ("‹ Task", Escape).
      : <OutputBackContext.Provider value={`task:${taskId}`}><TaskPaneBody detail={data} viewerId={viewerId} actionError={actionError} /></OutputBackContext.Provider>}
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
  // One box (W14): it steers while it runs or needs the viewer, and asks a follow-up once Done; a Closed task has none.
  const done = view.word === "Done";
  const steerable = view.word === "Working" || view.word === "Needs you" || done;
  const effectRows = effectFooter(effects, closed);
  const live = view.word === "Working";
  const trace = useLatestTrace(detail, live);
  const tools = trace && !("error" in trace) ? trace.recentTools : null;
  // The live run this viewer may watch and drive: on the workspace VM only, in the viewer's own workspace (GET /tasks/{id}
  // `browserRunId`; the shared type does not carry it yet). A run on its own task VM has none: its input would 409.
  const runId = (detail as TaskDetail & { browserRunId?: string | null }).browserRunId;
  // Top to bottom (dev walk 13, W13-5/W13-9): while it runs, its latest step; then what it said and made, once each
  // (the result's file cards are the output chips); one quiet times line; Steps, the one box and Details right under it.
  return <div className="room-context-body flex min-h-0 flex-col gap-3">
    <TaskHeader task={task} viewerId={viewerId} error={actionError} />
    {live && <p role="status" aria-live="polite" className="flex items-center gap-2 px-1 text-ui text-muted-foreground">
      <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-info motion-safe:animate-pulse" />{tools ? stepNow(tools, task.title) : "Getting started…"}</p>}
    {live && runId && tools?.some(isBrowserTool) && <LiveBrowser runId={runId} />}
    {open && <DecisionCard key={open.id} task={task} question={open} ended={closed} />}
    {decided.length > 0 && <ul aria-label="Decisions" className="grid gap-1 px-1 text-ui text-muted-foreground">
      {decided.map((d) => <li key={d.id}>{d.line}</li>)}
    </ul>}
    <SessionChat sessionId={detail.taskRoomId} sessionOwnerId={task.ownerUserId ?? ""} words={TRANSCRIPT_WORDS}
      openChat={async (roomId) => ({ roomId })} readOnly bare hideMessage={hideCard} />
    {/* What it made, one chip each (a draft waiting on review shows in its card above). */}
    {!!task.outputs?.length && <section aria-label="Outputs" className="px-1"><TaskOutputs task={task} /></section>}
    {effectRows.length > 0 && <ul aria-label="Effects" className="grid gap-1 px-1 text-ui">
      {effectRows.map(({ key, name, line, warn }) => <li key={key} className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-semibold">{name}</span>
        <span className={warn ? "text-warn" : "text-muted-foreground"}>{line}</span>
      </li>)}
    </ul>}
    <TimesLine detail={detail} word={view.word} />
    <TaskSteps detail={detail} live={live} trace={trace} />
    {/* The pane's own box; never beside a typed answer. */}
    {steerable && !(open?.canAnswer && !open.options.length) && <AddNote task={task} followUp={done} />}
    <TaskDetails detail={detail} />
  </div>;
}

/** A step in the browser (the bridge's `browser_*` tools, and `computer_use` on its screen). */
export const isBrowserTool = (tool: unknown): boolean => {
  const name = typeof tool === "string" ? tool : (tool as { name?: unknown } | null)?.name;
  return typeof name === "string" && (name.startsWith("browser_") || name === "computer_use");
};

/** The Worker's browser tab while it browses: its frames are cast to the room the task came from, and a click, scroll
 *  or keys on the picture go to that tab, so the owner can watch it and take over (the agent waits while they do).
 *  Only for a run the server says this viewer may drive (`browserRunId`), and only while its recent steps are in the
 *  browser: the viewing beat starts a stopped browser. ponytail: a run-scoped beat if a lingering pane ever restarts one. */
function LiveBrowser({ runId }: { runId: string }): ReactElement {
  return <section aria-label="Browser" className="grid gap-1.5 px-1">
    <div className="relative aspect-[16/10] overflow-hidden rounded-lg border" style={{ borderColor: "var(--edge)" }}>
      <BrowserFrameView runId={runId} interactive
        empty={<p role="status" className="text-ui text-muted-foreground">Waiting for the page…</p>} />
    </div>
    <p className="text-caption text-muted-foreground">Live. Click or type on the page to take over; Lightbulb waits while you do.</p>
  </section>;
}

/** "Started 3:19 · Done 3:21": one quiet line in place of the transcript's day and lifecycle rules (W12-17). */
function TimesLine({ detail, word }: { detail: TaskDetail; word: string }): ReactElement | null {
  const first = detail.attempts.reduce<TaskDetail["attempts"][number] | null>((a, b) => (!a || b.n < a.n ? b : a), null);
  const last = detail.attempts.reduce<TaskDetail["attempts"][number] | null>((a, b) => (!a || b.n > a.n ? b : a), null);
  if (!first) return null;
  const ended = (word === "Done" || word === "Closed") && last?.endedAt ? ` · ${word} ${time(last.endedAt)}` : "";
  return <p className="px-1 text-caption text-muted-foreground">Started {time(first.startedAt)}{ended}</p>;
}

/** The latest run's step records: read once, then every few seconds while it runs (the live line and Steps share it). */
function useLatestTrace(detail: TaskDetail, live: boolean): TaskTrace | { error: true } | null {
  const { tasks } = useLiveEvents();
  const latest = detail.attempts.reduce((n, a) => Math.max(n, a.n), 0);
  const [trace, setTrace] = useState<TaskTrace | { error: true } | null>(null);
  const taskId = detail.task.taskId;
  useEffect(() => {
    if (!latest) return;
    let alive = true;
    const read = () => void tasks.trace(taskId, latest).then((t) => { if (alive) setTrace(t); }, () => { if (alive) setTrace({ error: true }); });
    read();
    const timer = live ? window.setInterval(read, 5000) : undefined;
    return () => { alive = false; window.clearInterval(timer); };
  }, [live, latest, taskId, tasks]);
  return trace;
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
  return <section aria-label="Thread status" className="grid gap-1 px-1 empty:hidden">
    {view.reason && <p className="text-ui text-muted-foreground">{view.reason}</p>}
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </section>;
}

/** How long it has been working, "0:48" (since it last changed state, as the list's live line), ticking each second.
 *  In the header's meta, which never truncates (W12-17). */
export function Ticking({ since }: { since: string }): ReactElement {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <time dateTime={since} className="tabular-nums">{clock(Math.max(0, Math.floor((now - Date.parse(since)) / 1000)) || 0)}</time>;
}

/** 48 → "0:48", 725 → "12:05", 3725 → "1:02:05". */
export const clock = (s: number): string => {
  const two = (n: number) => String(n).padStart(2, "0");
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
  return h ? `${h}:${two(m)}:${two(s % 60)}` : `${m}:${two(s % 60)}`;
};

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
  if (live && confirmStop) return <span role="group" aria-label="Stop this thread?" className="inline-flex items-center gap-2 text-ui">
    <span className="whitespace-nowrap">Stop this thread?</span>
    <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => void press("stop")}>Stop it</Button>
    <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmStop(false)}>Keep going</Button>
  </span>;
  // Stop and Retry each keep their own slot (UX audit 2, P0); an empty slot takes no room. Reopen undoes a Stop or a
  // Close and runs nothing.
  return <span className="flex items-center gap-1">
    <span data-action-slot="stop" className="empty:hidden">
      {live && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setConfirmStop(true)}>Stop</Button>}
    </span>
    {/* A failed task runs again from here (the owner's; the server's view offers it). */}
    <span data-action-slot="retry" className="empty:hidden">
      {view.action === "retry" && <Button type="button" size="sm" disabled={busy} onClick={() => void press("retry")}>Retry</Button>}
    </span>
    {/* Close or Reopen: the only lifecycle choice left, so a quiet text action, not a ⋯ menu of one item beside ✕
        (W14). Never "Mark done" beside the "Done" word (W13-3); a Done task closes itself after a quiet week anyway. */}
    <span data-action-slot="close" className="empty:hidden">
      {(!live || task.status === "resolved") && <Button type="button" size="sm" variant="ghost" className="text-muted-foreground" disabled={busy}
        
        onClick={() => void press(task.status === "resolved" ? "reopen" : "resolve")}>{task.status === "resolved" ? "Reopen" : "Close thread"}</Button>}
    </span>
  </span>;
}

/** Steering when the room's composer isn't on screen: one line, Enter sends it into the task's room. */
function AddNote({ task, followUp }: { task: TaskSummary; followUp: boolean }): ReactElement {
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
    <Input aria-label={followUp ? "Follow up on this thread" : "Steer this thread"} placeholder={followUp ? "Ask a follow-up…" : "Steer this thread…"}
      value={text} maxLength={4000} disabled={pending}
      onChange={(event) => setText(event.target.value)} />
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
  </form>;
}

/** A fold's toggle with our chevron (turns when open), not the browser's triangle (W14). Its parent is `group`. */
function Summary({ children }: { children: ReactNode }): ReactElement {
  return <summary className="inline-flex cursor-pointer list-none items-center gap-1 rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
    <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 transition-transform duration-(--dur-fast) group-open:rotate-90 motion-reduce:transition-none" />{children}
  </summary>;
}

export function TaskDetails({ detail }: { detail: TaskDetail }): ReactElement {
  const { task, attempts } = detail;
  const cost = task.budgetUsd != null || task.costUsd
    ? `${money(task.costUsd ?? 0)}${task.budgetUsd != null ? ` of ${money(task.budgetUsd)} budget` : ""}` : null;
  return <details className="group px-1 text-ui">
    <Summary>Details</Summary>
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      <dt className="text-muted-foreground">Runs</dt><dd>{attempts.length}</dd>
      {task.runsOn && <><dt className="text-muted-foreground">Runs on</dt><dd>Lightbulb cloud</dd></>}
      {cost && <><dt className="text-muted-foreground">Cost</dt><dd>{cost}</dd></>}
    </dl>
  </details>;
}

/** The latest run's steps, under the result, counted in the heading ("Steps · 2"), in plain words (W12-7). Open while
 *  it runs (W13-5); a person's toggle wins after that. */
function TaskSteps({ detail, live, trace }: { detail: TaskDetail; live: boolean; trace: TaskTrace | { error: true } | null }): ReactElement | null {
  const latest = detail.attempts.reduce((n, a) => Math.max(n, a.n), 0);
  if (!latest) return null;
  const done = trace && !("error" in trace) ? stepLines(trace.recentTools) : null;
  // Still running, its work step says what it is doing now, not "Worked on it" (W16-11).
  const lines = done && live && trace && !("error" in trace) && done.at(-1) === "Worked on it"
    ? [...done.slice(0, -1), stepNow(trace.recentTools, detail.task.title)] : done;
  return <details className="group px-1 text-ui" open={live || undefined}>
    <Summary>Steps{lines ? ` · ${lines.length}` : ""}{detail.attempts.length > 1 ? ` · run ${latest}` : ""}</Summary>
    {trace == null ? <p className="text-muted-foreground">Loading…</p>
      : !lines ? <p className="text-muted-foreground">The steps aren&apos;t available.</p>
      : !lines.length ? <p className="text-muted-foreground">No steps yet.</p>
      : <ol className="mt-1 max-h-64 overflow-auto text-caption" aria-label={`Run ${latest} steps`}>
        {lines.map((line, i) => <li key={i} className="truncate">{line}</li>)}
      </ol>}
  </details>;
}
