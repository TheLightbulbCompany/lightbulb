"use client";

// Home: every request the viewer can see, under the four status words (Needs you · Working · Done ·
// Closed) from the shared `overviewGroups`, so Home can't drift from the words on its rows. Needs you is the
// complete server read the rail badge counts (useNeedsYouTasks); the rest come from the paged workspace list.

import { useState, type ReactElement, type ReactNode } from "react";
import { overviewGroups, useAutomations, useOlderTasks, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { openPanel } from "@/components/workspace/workspaceController";
import { useAutomationSource } from "./automations";
import { NeedsYouItem, OverviewRow } from "./TaskOverview";
import { useNeedsYouTasks } from "./useRoomThreads";

const WORKSPACE: TaskScope = { kind: "workspace" };
/** Done shows this many before "Show all". */
export const DONE_FOLD = 5;

/** Home, "your company today", top to bottom: Needs you, then the `between` slots (your mentions, Ideas, the feed),
 *  then Working, Done and Closed (collapsed). The slots render while the requests are still loading. */
export function HomeWork({ viewerId, onOpen, between, asks, askCount = 0 }: {
  viewerId: string; onOpen: (taskId: string) => void; between?: ReactNode;
  /** Open asks that are not tasks (a website sign-in, a browser extension): in Needs you and in its count. */
  asks?: ReactNode; askCount?: number;
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
  const automations = scheduled?.automations.length ?? 0;
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
          <button type="button" aria-expanded={doneOpen} className="hover:underline" onClick={() => setDoneOpen(!doneOpen)}>Done · {done.length}</button>
        </h2>
        {doneOpen && rows(doneAll ? done : done.slice(0, DONE_FOLD))}
        {doneOpen && done.length > DONE_FOLD && <button type="button" aria-expanded={doneAll} className="mt-1 text-ui text-muted-foreground underline hover:text-foreground"
          onClick={() => setDoneAll(!doneAll)}>{doneAll ? "Show fewer" : `Show all ${done.length}`}</button>}
      </section>}
      {/* Resolved, stopped, dismissed and someone else's failed requests: collapsed until asked for. */}
      {closed.length > 0 && <section aria-labelledby="home-closed">
        <h2 id="home-closed" className="text-ui font-semibold">
          <button type="button" aria-expanded={closedOpen} className="hover:underline" onClick={() => setClosedOpen(!closedOpen)}>Closed · {closed.length}</button>
        </h2>
        {closedOpen && rows(closed)}
      </section>}
      {hasOlder && <button type="button" className="justify-self-start text-ui text-muted-foreground underline hover:text-foreground"
        disabled={loading} onClick={() => void loadOlder()}>{loading ? "Loading…" : "Load older tasks"}</button>}
    </>}
    <p className="text-ui text-muted-foreground">
      {scheduled ? `${automations} scheduled ${automations === 1 ? "automation" : "automations"}` : "Scheduled work"} ·{" "}
      <button type="button" className="underline hover:text-foreground" onClick={() => openPanel("teams-routines", {}, { pushUrl: true })}>Automations</button>
    </p>
  </div>;
}
