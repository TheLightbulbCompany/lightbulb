"use client";

// Home's top half: every request the viewer can see, cut into Needs you · Working · Done from the server's
// `shownGroup` (homeSections), with the Work overview's own rows. Needs you is the complete server read the rail
// badge counts (useNeedsYouTasks); Working and Done come from the paged workspace list.

import { useState, type ReactElement } from "react";
import { useAutomations, useOlderTasks, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { homeSections } from "./homeSections";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { openPanel } from "@/components/workspace/workspaceController";
import { useAutomationSource } from "./automations";
import { OverviewRow } from "./TaskOverview";
import { useNeedsYouTasks } from "./useRoomThreads";

const WORKSPACE: TaskScope = { kind: "workspace" };
/** Done shows this many before "Show all". */
export const DONE_FOLD = 5;

export function HomeWork({ viewerId, onOpen }: { viewerId: string; onOpen: (taskId: string) => void }): ReactElement {
  const { tasks } = useLiveEvents();
  const { data, error, mutate } = useTaskList(tasks, WORKSPACE);
  const { older, hasOlder, loading, loadOlder } = useOlderTasks(tasks, data, WORKSPACE);
  const needs = useNeedsYouTasks(WORKSPACE);
  const { data: scheduled } = useAutomations(useAutomationSource());
  const [doneOpen, setDoneOpen] = useState(false);
  if (!data || !needs.loaded) return <div className="home-work">{error || needs.error
    ? <p role="alert" className="text-ui text-muted-foreground">Requests couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => { void mutate(); needs.retry(); }}>Retry</button></p>
    : <p role="status" className="text-ui text-muted-foreground">Loading…</p>}</div>;
  const needsYou = needs.tasks;
  const { working, done } = homeSections([...data.tasks, ...older]);
  const rows = (list: TaskSummary[]) => <ul className="mt-1 grid">{list.map((t) => <OverviewRow key={t.taskId} task={t} viewerId={viewerId} onOpen={onOpen} />)}</ul>;
  const automations = scheduled?.automations.length ?? 0;
  return <div className="home-work">
    <section aria-labelledby="home-needs-you">
      <h2 id="home-needs-you" className="text-ui font-semibold">Needs you · {needsYou.length}</h2>
      {needsYou.length ? rows(needsYou)
        : <p className="mt-1 rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">Nothing needs you</p>}
    </section>
    {working.length > 0 && <section aria-labelledby="home-working">
      <h2 id="home-working" className="text-ui font-semibold">Working · {working.length}</h2>
      {rows(working)}
    </section>}
    {done.length > 0 && <section aria-labelledby="home-done">
      <h2 id="home-done" className="text-ui font-semibold">Done · {done.length}</h2>
      {rows(doneOpen ? done : done.slice(0, DONE_FOLD))}
      {done.length > DONE_FOLD && <button type="button" aria-expanded={doneOpen} className="mt-1 text-ui text-muted-foreground underline hover:text-foreground"
        onClick={() => setDoneOpen(!doneOpen)}>{doneOpen ? "Show fewer" : `Show all ${done.length}`}</button>}
    </section>}
    {hasOlder && <button type="button" className="justify-self-start text-ui text-muted-foreground underline hover:text-foreground"
      disabled={loading} onClick={() => void loadOlder()}>{loading ? "Loading…" : "Load older requests"}</button>}
    <p className="text-ui text-muted-foreground">
      {scheduled ? `${automations} scheduled ${automations === 1 ? "automation" : "automations"}` : "Scheduled work"} ·{" "}
      <button type="button" className="underline hover:text-foreground" onClick={() => openPanel("teams-routines", {}, { pushUrl: true })}>Automations</button>
    </p>
  </div>;
}
