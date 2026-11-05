"use client";

// The overview beside the chat (mockup step 3): threads grouped Waiting on you · Working · Ready for
// review · Applying · Idle · Resolved, from the server's `shownGroup`. A second tab lists Scheduled work.

import { useState, type ReactElement } from "react";
import { MoreHorizontal } from "lucide-react";
import { overviewGroups, summaryView, taskSummaryLine, useOlderTasks, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { PanelTabs } from "@/components/teams/shell";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { copyText, taskPermalink } from "@/components/messaging/messageLinks";
import { TaskStatus } from "./TaskCard";
import { ScheduledList } from "./ScheduledList";
import { useTaskActions } from "./useTaskActions";

const RESOLVED_OPEN_KEY = "lightbulb.overview.resolvedOpen";
const PLACEHOLDERS: Partial<Record<string, string>> = { waiting_on_you: "Nothing needs you", resolved: "Nothing resolved yet" };

function readResolvedOpen(viewerId: string): boolean {
  try { return localStorage.getItem(`${RESOLVED_OPEN_KEY}:${viewerId}`) === "1"; } catch { return false; }
}
function writeResolvedOpen(viewerId: string, open: boolean): void {
  try { localStorage.setItem(`${RESOLVED_OPEN_KEY}:${viewerId}`, open ? "1" : "0"); } catch { /* private mode: per-session only */ }
}

export function TaskOverview({ scope, viewerId, onOpen }: {
  scope: TaskScope; viewerId: string; onOpen: (taskId: string) => void;
}): ReactElement {
  const [tab, setTab] = useState<"overview" | "scheduled">("overview");
  return <div className="grid gap-3">
    <PanelTabs ariaLabel="Threads" tabs={[{ value: "overview", label: "Overview" }, { value: "scheduled", label: "Scheduled" }]}
      value={tab} onValueChange={(v) => setTab(v as "overview" | "scheduled")} />
    {tab === "overview" ? <OverviewGroups scope={scope} viewerId={viewerId} onOpen={onOpen} /> : <ScheduledList scope={scope} onOpen={onOpen} />}
  </div>;
}

function OverviewGroups({ scope, viewerId, onOpen }: { scope: TaskScope; viewerId: string; onOpen: (taskId: string) => void }): ReactElement {
  const { tasks } = useLiveEvents();
  const { data, error } = useTaskList(tasks, scope);
  const { older, hasOlder, loading, loadOlder } = useOlderTasks(tasks, data, scope);
  const [resolvedOpen, setResolvedOpen] = useState(() => readResolvedOpen(viewerId));
  if (!data) return <p role="status" className="text-ui text-muted-foreground">{error ? "Threads couldn't load. Try again." : "Loading…"}</p>;
  return <div className="grid gap-4">
    {overviewGroups([...data.tasks, ...older]).map((group) => {
      const collapsible = group.id === "resolved";
      const open = !collapsible || resolvedOpen;
      const headingId = `overview-${group.id}`;
      return <section key={group.id} aria-labelledby={headingId}>
        <h3 id={headingId} className="flex items-center gap-2 text-ui font-semibold">
          {collapsible
            ? <button type="button" aria-expanded={open} className="hover:underline"
              onClick={() => { setResolvedOpen(!open); writeResolvedOpen(viewerId, !open); }}>{group.label} · {group.tasks.length}</button>
            : <>{group.label} · {group.tasks.length}</>}
        </h3>
        {open && (group.tasks.length
          ? <ul className="mt-1 grid">{group.tasks.map((t) => <OverviewRow key={t.taskId} task={t} viewerId={viewerId} onOpen={onOpen} />)}</ul>
          : PLACEHOLDERS[group.id] && <p className="mt-1 rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">{PLACEHOLDERS[group.id]}</p>)}
      </section>;
    })}
    {hasOlder && <button type="button" className="justify-self-start text-ui text-muted-foreground underline hover:text-foreground"
      disabled={loading} onClick={() => void loadOlder()}>{loading ? "Loading…" : "Load older threads"}</button>}
  </div>;
}

export function OverviewRow({ task, viewerId, onOpen }: { task: TaskSummary; viewerId: string; onOpen: (taskId: string) => void }): ReactElement {
  const { act } = useTaskActions("overview");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const view = summaryView(task, viewerId);
  const { agentNames } = useLiveEvents();
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
  return <li className="relative flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent">
    <div className="min-w-0 flex-1">
      <button type="button" onClick={() => onOpen(task.taskId)} aria-label={`Open ${task.title}, ${view.word}`}
        className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:content-['']">{task.title}</button>
      <p className="truncate text-ui text-muted-foreground">{taskSummaryLine(task, viewerId, agentNames)}</p>
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
      {copied && <p role="status" className="text-ui text-muted-foreground">Link copied</p>}
    </div>
    <TaskStatus task={task} viewerId={viewerId} />
    <div className="relative z-10 flex items-center">
      {task.status === "resolved" && <Button type="button" size="sm" variant="ghost" onClick={() => void press("reopen")}>Reopen</Button>}
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button type="button" size="icon-sm" variant="ghost" aria-label={`More for ${task.title}`}><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => onOpen(task.taskId)}>Open</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => void copyLink()}>Copy link</DropdownMenuItem>
          {task.status !== "resolved" && <DropdownMenuItem onSelect={() => void press("resolve")}>Resolve</DropdownMenuItem>}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </li>;
}
