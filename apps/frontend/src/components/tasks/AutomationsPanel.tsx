"use client";

// Work › Automations with the switch on (mockup step 11): one object per automation with its schedule in
// the viewer's time zone, its instructions, Pause and Run now, and a history in which every run is its
// own thread. Off, today's RoutinesPanel renders untouched; "Older automations" reaches it when on.

import { useState, type ReactElement } from "react";
import { ArrowLeft } from "lucide-react";
import { formatSchedule, specialistLabel, summaryView, type AutomationView } from "@lightbulb/shared";
import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";
import { Button } from "@/components/ui/button";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { openTaskPane } from "@/components/workspace/workspaceController";
import { statusLine, useAutomations } from "./ScheduledList";
import { budgetLine, useAutomation, useAutomationActions, useAutomationRuns, viewerTimeZone } from "./automations";

export function AutomationsPanel({ viewerTz = viewerTimeZone(), onOpenTask = openTaskPane }: {
  viewerTz?: string; onOpenTask?: (taskId: string) => void;
}): ReactElement {
  const on = useSuperAgent();
  const [legacy, setLegacy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  if (!on || legacy) return <>
    {on && <p className="px-6 pt-3 text-ui"><button type="button" className="underline" onClick={() => setLegacy(false)}>‹ Automations</button></p>}
    <RoutinesPanel />
  </>;
  return <div className="px-6 py-4">
    {selected
      ? <AutomationDetail id={selected} viewerTz={viewerTz} onBack={() => setSelected(null)} onOpenTask={onOpenTask} />
      : <AutomationList viewerTz={viewerTz} onSelect={setSelected} onOlder={() => setLegacy(true)} />}
  </div>;
}

function scheduleLine(a: AutomationView, viewerTz: string): string {
  if (!a.schedule) return "No schedule";
  const words = formatSchedule(a.schedule.cron, a.schedule.tz, viewerTz);
  return a.schedule.tz === viewerTz ? words : `${words} (your time)`;
}

function AutomationList({ viewerTz, onSelect, onOlder }: { viewerTz: string; onSelect: (id: string) => void; onOlder: () => void }): ReactElement {
  const { data, error } = useAutomations();
  const { act } = useAutomationActions();
  const [note, setNote] = useState<string | null>(null);
  const rows = data?.automations ?? [];
  return <div className="grid gap-3">
    <div className="flex items-baseline justify-between gap-3">
      <h2 className="font-semibold">Automations</h2>
      <button type="button" className="text-ui text-muted-foreground underline" onClick={onOlder}>Older automations</button>
    </div>
    {!data && <p role="status" className="text-ui text-muted-foreground">{error ? "Automations aren't available yet." : "Loading…"}</p>}
    {data && rows.length === 0 && <p className="rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      Ask @lightbulb to put recurring work on a schedule.
    </p>}
    {rows.length > 0 && <ul className="grid">{rows.map((a) => <li key={a.id} className="relative flex items-center gap-3 rounded-md px-2 py-2 hover:bg-accent">
      <div className="min-w-0 flex-1">
        <button type="button" className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:content-['']" onClick={() => onSelect(a.id)}>{a.name}</button>
        <p className="truncate text-ui text-muted-foreground">{[scheduleLine(a, viewerTz), a.deliverTo && a.deliverTo !== "none" ? `posts to ${a.deliverTo}` : null, statusLine(a)].filter(Boolean).join(" · ")}</p>
      </div>
      <Button type="button" size="sm" variant="outline" className="relative z-10" disabled={a.status === "draft"}
        onClick={async () => { try { await act(a.id, "run"); setNote(`${a.name} is running.`); } catch { setNote(`${a.name} couldn't run. Try again.`); } }}>Run now</Button>
    </li>)}</ul>}
    {note && <p role="status" className="text-ui text-muted-foreground">{note}</p>}
  </div>;
}

function AutomationDetail({ id, viewerTz, onBack, onOpenTask }: {
  id: string; viewerTz: string; onBack: () => void; onOpenTask: (taskId: string) => void;
}): ReactElement {
  const { data: a, error } = useAutomation(id);
  const { data: runs } = useAutomationRuns(id);
  const { act } = useAutomationActions();
  const [note, setNote] = useState<string | null>(null);
  const press = async (verb: "pause" | "resume" | "run") => {
    try { await act(id, verb); setNote(verb === "run" ? "Running now." : null); }
    catch { setNote("That didn't go through. Try again."); }
  };
  if (!a) return <div className="grid gap-3">
    <Button variant="ghost" aria-label="Back to automations" onClick={onBack}><ArrowLeft aria-hidden="true" />Automations</Button>
    <p role="status" className="text-ui text-muted-foreground">{error ? "This automation isn't available." : "Loading…"}</p>
  </div>;
  const paused = a.status === "paused" || a.status === "paused_system";
  return <div className="grid gap-3">
    <Button variant="ghost" aria-label="Back to automations" onClick={onBack}><ArrowLeft aria-hidden="true" />Automations</Button>
    <header className="grid gap-1">
      <h2 className="font-semibold">{a.name}</h2>
      <p className="text-ui text-muted-foreground">{[scheduleLine(a, viewerTz), specialistLabel(a.specialistId), budgetLine(a)].filter(Boolean).join(" · ")}</p>
      {statusLine(a) && <p className="text-ui text-warn">{statusLine(a)}</p>}
    </header>
    <div className="flex flex-wrap gap-2">
      {a.status !== "draft" && <Button type="button" size="sm" variant="outline" onClick={() => void press(paused ? "resume" : "pause")}>{paused ? "Resume" : "Pause"}</Button>}
      <Button type="button" size="sm" disabled={a.status === "draft"} onClick={() => void press("run")}>Run now</Button>
    </div>
    {note && <p role="status" className="text-ui text-muted-foreground">{note}</p>}
    <section aria-label="Instructions" className="editorial-card px-3 py-2">
      <h3 className="label-caps">Instructions</h3>
      <p className="whitespace-pre-wrap text-ui">{a.instructions}</p>
    </section>
    <section aria-label="History" className="grid gap-1">
      <h3 className="label-caps">History</h3>
      {!runs?.runs.length
        ? <p className="text-ui text-muted-foreground">No runs yet.</p>
        : <ul className="grid">{runs.runs.map((t) => <li key={t.taskId}>
          <button type="button" className="flex w-full items-center gap-3 rounded-md px-2 py-1.5 text-left hover:bg-accent" onClick={() => onOpenTask(t.taskId)}>
            <span className="min-w-0 flex-1 truncate">{t.title}</span>
            <span className="text-ui text-muted-foreground">{summaryView(t, a.ownerUserId).word}</span>
          </button>
        </li>)}</ul>}
    </section>
  </div>;
}
