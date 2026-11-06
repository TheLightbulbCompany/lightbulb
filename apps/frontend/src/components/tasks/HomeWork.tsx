"use client";

// Home: every request the viewer can see, under the four status words (Needs you · Working · Done ·
// Closed) from the shared `overviewGroups`, so Home can't drift from the words on its rows. Needs you is the
// complete server read the rail badge counts (useNeedsYouTasks); the rest come from the paged workspace list.

import { useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { ChevronRight, Clock } from "lucide-react";
import { deliveryRoomId, overviewGroups, roomDisplayName, useAutomations, useOlderTasks, useTaskList, type AutomationView, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { openPanel } from "@/components/workspace/workspaceController";
import { pushSearchParams } from "@/lib/shellNav";
import { scheduleWords, useAutomationSource, viewerTimeZone } from "./automations";
import { NeedsYouItem, OverviewRow } from "./TaskOverview";
import { useNeedsYouTasks } from "./useRoomThreads";

const WORKSPACE: TaskScope = { kind: "workspace" };
/** Done shows this many before "Show all". */
export const DONE_FOLD = 5;

/** Home, "your company today", top to bottom: Needs you, then the `between` slots (your mentions, Ideas, the feed),
 *  then Working, Done and Closed (collapsed). The slots render while the requests are still loading. */
export function HomeWork({ viewerId, onOpen, between, asks, askCount = 0, scheduledOpen = false }: {
  viewerId: string; onOpen: (taskId: string) => void; between?: ReactNode;
  /** Open asks that are not tasks (a website sign-in, a browser extension): in Needs you and in its count. */
  asks?: ReactNode; askCount?: number;
  /** Open the Scheduled group and bring it into view (`?panel=activity&section=scheduled`, an old Automations link). */
  scheduledOpen?: boolean;
}): ReactElement {
  const { tasks } = useLiveEvents();
  const { data, error, mutate } = useTaskList(tasks, WORKSPACE);
  const { older, hasOlder, loading, loadOlder } = useOlderTasks(tasks, data, WORKSPACE);
  const needs = useNeedsYouTasks(WORKSPACE);
  const { data: scheduled } = useAutomations(useAutomationSource());
  const [doneOpen, setDoneOpen] = useState(false);
  const [doneAll, setDoneAll] = useState(false);
  const [closedOpen, setClosedOpen] = useState(false);
  const rows = (list: TaskSummary[]) => <ul className="mt-1 grid">{list.map((t) => <OverviewRow key={t.taskId} task={t} viewerId={viewerId} onOpen={onOpen} />)}</ul>;
  const failed = error || needs.error;
  const status = failed
    ? <p role="alert" className="text-ui text-muted-foreground">Tasks couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => { void mutate(); needs.retry(); }}>Retry</button></p>
    : <p role="status" className="text-ui text-muted-foreground">Loading…</p>;
  const needsYou = needs.tasks;
  const groups = overviewGroups([...(data?.tasks ?? []), ...older]);
  const newestFirst = (list: TaskSummary[]) => [...list].sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));
  const of = (id: "working" | "done" | "closed") => groups.find((g) => g.id === id)?.tasks ?? [];
  const working = of("working"), done = newestFirst(of("done")), closed = newestFirst(of("closed"));
  // Every automation the viewer can see, in every project and DM; the builtin jobs live in Settings › Workspace.
  const automations = (scheduled?.automations ?? []).filter((a) => !a.id.startsWith("builtin:"));
  return <div className="home-work">
    {/* The one count (useNeedsYouCount): the decision cards here plus the non-task asks, as the badge and Home say. */}
    {needs.loaded ? <section aria-labelledby="home-needs-you">
      <h2 id="home-needs-you" className="text-ui font-semibold">Needs you · {needsYou.length + askCount}</h2>
      {needsYou.length > 0 && <ul className="mt-1 grid gap-2">{needsYou.map((t) => <NeedsYouItem key={t.taskId} task={t} viewerId={viewerId} onOpen={onOpen} />)}</ul>}
      {askCount > 0 && asks}
      {!needsYou.length && !askCount && <p className="mt-1 rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">Approvals and questions for you land here.</p>}
    </section> : status}
    {between}
    {!data ? (needs.loaded ? status : null) : <>
      {working.length > 0 && <section aria-labelledby="home-working">
        <h2 id="home-working" className="text-ui font-semibold">Working · {working.length}</h2>
        {rows(working)}
      </section>}
      {/* Done collapses by default, like Closed (07 §2): Activity leads with what needs you and what is moving. */}
      {done.length > 0 && <section aria-labelledby="home-done">
        <h2 id="home-done" className="text-ui font-semibold">
          <GroupToggle open={doneOpen} onToggle={() => setDoneOpen(!doneOpen)}>Done · {done.length}</GroupToggle>
        </h2>
        {doneOpen && rows(doneAll ? done : done.slice(0, DONE_FOLD))}
        {doneOpen && done.length > DONE_FOLD && <button type="button" aria-expanded={doneAll} className="mt-1 text-ui text-muted-foreground underline hover:text-foreground"
          onClick={() => setDoneAll(!doneAll)}>{doneAll ? "Show fewer" : `Show all ${done.length}`}</button>}
      </section>}
      {/* Resolved, stopped, dismissed and someone else's failed requests: collapsed until asked for. */}
      {closed.length > 0 && <section aria-labelledby="home-closed">
        <h2 id="home-closed" className="text-ui font-semibold">
          <GroupToggle open={closedOpen} onToggle={() => setClosedOpen(!closedOpen)}>Closed · {closed.length}</GroupToggle>
        </h2>
        {closedOpen && rows(closed)}
      </section>}
      {/* Older pages mostly add Done and Closed rows: while either group is there but collapsed, the link waits until one
          is open, so a bare count never sits over a load link (W11-4). */}
      {hasOlder && (doneOpen || closedOpen || (!done.length && !closed.length)) && <button type="button" className="justify-self-start text-ui text-muted-foreground underline hover:text-foreground"
        disabled={loading} onClick={() => void loadOlder()}>{loading ? "Loading…" : "Load older tasks"}</button>}
    </>}
    {/* Work dissolved (UX slice 4): what was scheduled is a group here, and the old task board is one link away. */}
    {scheduled && <ScheduledGroup automations={automations} initiallyOpen={scheduledOpen} />}
    <button type="button" className="justify-self-start text-ui text-muted-foreground hover:text-foreground hover:underline"
      onClick={() => openPanel("teams-issues", {}, { pushUrl: true })}>Older tasks (before 5 Nov) ›</button>
  </div>;
}

/** A collapsed group's heading: a chevron that turns when open, so a count with no rows reads as closed, not empty (W11-4). */
function GroupToggle({ open, onToggle, children }: { open: boolean; onToggle: () => void; children: ReactNode }): ReactElement {
  return <button type="button" aria-expanded={open} className="inline-flex items-center gap-1 hover:underline" onClick={onToggle}>
    <ChevronRight size={14} aria-hidden="true" className={`flex-none text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`} />{children}
  </button>;
}

/** Activity's Scheduled group: collapsed by default, one line per automation, and a row opens it in its project's pane
 *  (`?panel=chat&room=…&pane=automation&pane.id=…`), so a reload restores it. It counts what exists, never "none" beside
 *  one that does. */
function ScheduledGroup({ automations, initiallyOpen }: { automations: readonly AutomationView[]; initiallyOpen: boolean }): ReactElement {
  const [open, setOpen] = useState(initiallyOpen);
  // A link that asks for the group (again) opens it, adjusted during render; the effect only brings it into view.
  const [asked, setAsked] = useState(initiallyOpen);
  if (asked !== initiallyOpen) {
    setAsked(initiallyOpen);
    if (initiallyOpen) setOpen(true);
  }
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (initiallyOpen) ref.current?.scrollIntoView?.({ block: "start" });
  }, [initiallyOpen]);
  const rooms = useMessagingWorkspaceOptional()?.directoryProps.rooms;
  const tz = viewerTimeZone();
  const where = (a: AutomationView) => {
    const id = deliveryRoomId(a.deliverTo);
    const room = id ? rooms?.find((r) => r.roomId === id) : undefined;
    return room ? roomDisplayName(room, room.participants) : id ? null : "a DM";
  };
  const show = (a: AutomationView) => {
    const roomId = deliveryRoomId(a.deliverTo);
    if (roomId) pushSearchParams(new URLSearchParams({ panel: "chat", room: roomId, pane: "automation", "pane.id": a.id }));
    else openPanel("automation", { id: a.id }, { pushUrl: true });
  };
  return <section ref={ref} aria-labelledby="home-scheduled">
    <h2 id="home-scheduled" className="text-ui font-semibold">
      <GroupToggle open={open} onToggle={() => setOpen(!open)}>Scheduled · {automations.length}</GroupToggle>
    </h2>
    {open && (automations.length
      ? <ul className="mt-1 grid">{automations.map((a) => {
        const paused = a.status === "paused" || a.status === "paused_system";
        const line = [where(a), scheduleWords(a, tz)].filter(Boolean).join(" · ");
        return <li key={a.id}>
          <button type="button" className="flex min-h-[42px] w-full items-center gap-2 rounded-md px-2 text-left hover:bg-accent" onClick={() => show(a)}>
            <Clock size={14} aria-hidden="true" className="flex-none text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate"><span className="font-medium">{a.name}</span>
              {line && <span className="text-muted-foreground"> — {line}</span>}</span>
            <span className={paused ? "flex-none text-ui text-warn" : "flex-none text-ui text-muted-foreground"}>{paused ? "Paused" : a.status === "draft" ? "Draft" : "On"}</span>
          </button>
        </li>;
      })}</ul>
      : <p className="mt-1 text-ui text-muted-foreground">Ask Lightbulb in a project to put recurring work on a schedule, like a weekly summary.</p>)}
  </section>;
}
