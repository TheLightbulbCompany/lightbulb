"use client";

// The overview beside the chat (mockup step 3): requests grouped Needs you · Working · Done · Closed,
// from the server's `shownGroup`.

import { useState, type ReactElement } from "react";
import { MoreHorizontal } from "lucide-react";
import { overviewGroups, summaryView, taskSummaryLine, useOlderTasks, useTaskList, useTaskQuestions, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { copyText, taskPermalink } from "@/components/messaging/messageLinks";
import { DecisionCard } from "./DecisionCard";
import { ActorLine } from "./TaskCard";
import { useTaskActions } from "./useTaskActions";

const RESOLVED_OPEN_KEY = "lightbulb.overview.resolvedOpen";
const PLACEHOLDERS: Partial<Record<string, string>> = { needs_you: "Nothing needs you", closed: "Nothing closed yet" };

function readResolvedOpen(viewerId: string): boolean {
  try { return localStorage.getItem(`${RESOLVED_OPEN_KEY}:${viewerId}`) === "1"; } catch { return false; }
}
function writeResolvedOpen(viewerId: string, open: boolean): void {
  try { localStorage.setItem(`${RESOLVED_OPEN_KEY}:${viewerId}`, open ? "1" : "0"); } catch { /* private mode: per-session only */ }
}

/** A scope's tasks, grouped. Scheduled work is its own view (ScheduledList: a project pane's Scheduled, Activity's
 *  Scheduled group), never a second tab here (UX slice 4). */
export function TaskOverview({ scope, viewerId, onOpen }: {
  scope: TaskScope; viewerId: string; onOpen: (taskId: string) => void;
}): ReactElement {
  return <RequestGroups scope={scope} viewerId={viewerId} onOpen={onOpen} />;
}

/** The four status groups for a scope: the room's Requests column, and the Overview tab. */
export function RequestGroups({ scope, viewerId, onOpen }: { scope: TaskScope; viewerId: string; onOpen: (taskId: string) => void }): ReactElement {
  const { tasks } = useLiveEvents();
  const { data, error, mutate } = useTaskList(tasks, scope);
  const { older, hasOlder, loading, loadOlder } = useOlderTasks(tasks, data, scope);
  const [resolvedOpen, setResolvedOpen] = useState(() => readResolvedOpen(viewerId));
  // Done collapses by default too (07 §2), so the list leads with what needs you and what is moving.
  const [doneOpen, setDoneOpen] = useState(false);
  if (!data) return error
    ? <p role="alert" className="text-ui text-muted-foreground">Tasks couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button></p>
    : <p role="status" className="text-ui text-muted-foreground">Loading…</p>;
  return <div className="grid gap-4">
    {overviewGroups([...data.tasks, ...older]).map((group) => {
      const collapsible = group.collapsed || group.id === "done";
      const open = !collapsible || (group.id === "done" ? doneOpen : resolvedOpen);
      const toggle = () => { if (group.id === "done") setDoneOpen(!open); else { setResolvedOpen(!open); writeResolvedOpen(viewerId, !open); } };
      const headingId = `overview-${group.id}`;
      return <section key={group.id} aria-labelledby={headingId}>
        <h3 id={headingId} className="flex items-center gap-2 text-ui font-semibold">
          {collapsible
            ? <button type="button" aria-expanded={open} className="hover:underline"
              onClick={toggle}>{group.label} · {group.tasks.length}</button>
            : <>{group.label} · {group.tasks.length}</>}
        </h3>
        {open && (group.tasks.length
          ? <ul className={group.id === "needs_you" ? "mt-1 grid gap-2" : "mt-1 grid"}>{group.tasks.map((t) => group.id === "needs_you"
            ? <NeedsYouItem key={t.taskId} task={t} viewerId={viewerId} onOpen={onOpen} />
            : <OverviewRow key={t.taskId} task={t} viewerId={viewerId} onOpen={onOpen} />)}</ul>
          : PLACEHOLDERS[group.id] && <p className="mt-1 rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">{PLACEHOLDERS[group.id]}</p>)}
      </section>;
    })}
    {hasOlder && <button type="button" className="justify-self-start text-ui text-muted-foreground underline hover:text-foreground"
      disabled={loading} onClick={() => void loadOlder()}>{loading ? "Loading…" : "Load older tasks"}</button>}
  </div>;
}

/** A Needs you row: the one decision card, compact, answerable right here (UX audit slice 2); the plain row while the
 *  question loads, when it can't, or when the task needs you without a question (a draft ready for review). */
export function NeedsYouItem({ task, viewerId, onOpen, project }: {
  task: TaskSummary; viewerId: string; onOpen: (taskId: string) => void; project?: string | null;
}): ReactElement {
  const { tasks } = useLiveEvents();
  const qid = task.openQuestion?.qid ?? null;
  const { data } = useTaskQuestions(tasks, qid ? task.taskId : null);
  const question = data?.items.find((q) => q.id === qid && q.state === "open");
  return question
    ? <li><DecisionCard task={task} question={question} compact onOpen={() => onOpen(task.taskId)} /></li>
    : <OverviewRow task={task} viewerId={viewerId} onOpen={onOpen} project={project} />;
}

export function OverviewRow({ task, viewerId, onOpen, project }: {
  task: TaskSummary; viewerId: string; onOpen: (taskId: string) => void;
  /** The project's name, muted after the title, where the list spans projects (Activity). */
  project?: string | null;
}): ReactElement {
  const { act } = useTaskActions("overview");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const view = summaryView(task, viewerId);
  const line = taskSummaryLine(task, viewerId);
  const copyLink = async () => {
    const ok = await copyText(taskPermalink(task.taskId));
    setCopied(ok);
    setError(ok ? null : "Could not copy the link.");
    if (ok) setTimeout(() => setCopied(false), 2000);
  };
  const press = async (verb: "resolve" | "reopen") => {
    const res = await act(task, verb);
    setError(res.ok ? null : res.message);
  };
  // One line on a ~42 px pitch (Claude's Threads list, 07 §2): the title, what it needs or who is on it after an em
  // dash, the age on the right. No status word: the group heading already says it.
  const after = view.word === "Working" ? null : view.word === "Needs you" ? taskSummaryLine(task, viewerId)
    : view.reason && view.reason !== view.word ? view.reason : null;
  return <li className="group/row relative flex min-h-[42px] items-center gap-3 rounded-md px-2 hover:bg-accent">
    <p className="flex min-w-0 flex-1 items-baseline gap-1.5">
      <button type="button" onClick={() => onOpen(task.taskId)} aria-label={`Open ${task.title}, ${view.word}`}
        className="min-w-0 shrink truncate text-left font-semibold after:absolute after:inset-0 after:rounded-md after:content-[''] focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-ring">{task.title}</button>
      {project && <span className="max-w-[40%] flex-none truncate text-ui text-muted-foreground"><span aria-hidden="true">· </span>{project}</span>}
      {view.word === "Working"
        ? <><span aria-hidden="true" className="text-muted-foreground">—</span><ActorLine task={task} viewerId={viewerId} inline className="min-w-0" /></>
        : after && <span className="min-w-0 truncate text-ui text-muted-foreground"><span aria-hidden="true">— </span>{after}</span>}
    </p>
    {error && <span role="alert" className="shrink-0 text-ui text-err">{error}</span>}
    {copied && <span role="status" className="shrink-0 text-ui text-muted-foreground">Link copied</span>}
    {view.word !== "Working" && task.updatedAt && <time dateTime={task.updatedAt} title={new Date(task.updatedAt).toLocaleString()}
      className="shrink-0 text-caption tabular-nums text-muted-foreground">{timeAgo(task.updatedAt)}</time>}
    <div className="relative z-10 flex items-center">
      {task.status === "resolved" && <Button type="button" size="sm" variant="ghost" onClick={() => void press("reopen")}>Reopen</Button>}
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button type="button" size="icon-sm" variant="ghost" aria-label={`More for ${task.title}`}
          className="opacity-0 focus-visible:opacity-100 group-hover/row:opacity-100 data-[state=open]:opacity-100 [@media(hover:none)]:opacity-100"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => onOpen(task.taskId)}>Open</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => void copyLink()}>Copy link</DropdownMenuItem>
          {task.status !== "resolved" && <DropdownMenuItem onSelect={() => void press("resolve")}>Mark done</DropdownMenuItem>}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </li>;
}
