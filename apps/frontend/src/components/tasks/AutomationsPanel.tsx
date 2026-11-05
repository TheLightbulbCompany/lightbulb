"use client";

// Work › Automations with the switch on (mockup step 11): one object per automation with its schedule in
// the viewer's time zone, its instructions, Pause and Run now, and a history in which every run is its
// own thread. Off, today's RoutinesPanel renders untouched; "Older automations" reaches it when on.

import { useState, type ReactElement } from "react";
import { ArrowLeft } from "lucide-react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, automationRowLine, automationStatusLine, summaryView, useAutomation, useAutomationRuns, useAutomations, type AutomationView } from "@lightbulb/shared";
import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";
import { Button } from "@/components/ui/button";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { openTaskPane } from "@/components/workspace/workspaceController";
import {
  budgetLine, cronTime, deliveryWords, runNow, runNowReason, scheduleWords, useAutomationActions, useAutomationSource, useCanDecide, useCanEdit, useSpecialistLabel, viewerTimeZone, waitingWords, withCronTime,
} from "./automations";

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
      : <AutomationList viewerTz={viewerTz} onSelect={setSelected} onOlder={() => setLegacy(true)} onOpenTask={onOpenTask} />}
  </div>;
}

const scheduleLine = (a: AutomationView, viewerTz: string): string => scheduleWords(a, viewerTz) ?? "No schedule";

/** Run now, or "Running ›" while it is On and a run is going (opens it); a paused row never reads Running. A rule (lane S) has no Run now: it fires on a match.
 *  Disabled with its reason beside it when the viewer may not run it or it isn't on yet. */
function RunNowButton({ a, onOpenTask, onNote }: { a: AutomationView; onOpenTask: (taskId: string) => void; onNote: (note: string | null) => void }): ReactElement | null {
  const { act } = useAutomationActions();
  const canEdit = useCanEdit();
  if (a.match) return null;
  if (a.activeRunTaskId && a.status === "active") {
    return <Button type="button" size="sm" variant="ghost" className="relative z-10" onClick={() => onOpenTask(a.activeRunTaskId!)}>Running ›</Button>;
  }
  const reason = runNowReason(a, canEdit(a));
  return <span className="relative z-10 inline-flex items-center gap-2">
    {reason && <span className="text-caption text-muted-foreground">{reason}</span>}
    <Button type="button" size="sm" variant="outline" disabled={!!reason} aria-disabled={!!reason}
      onClick={async () => {
        const res = await runNow(act, a);
        if (res.kind === "running") onOpenTask(res.taskId);
        onNote(res.kind === "started" ? `${a.name} is running.` : res.kind === "note" ? res.message : null);
      }}>Run now</Button>
  </span>;
}

function AutomationList({ viewerTz, onSelect, onOlder, onOpenTask }: {
  viewerTz: string; onSelect: (id: string) => void; onOlder: () => void; onOpenTask: (taskId: string) => void;
}): ReactElement {
  const { data, error } = useAutomations(useAutomationSource());
  const [note, setNote] = useState<string | null>(null);
  const rows = data?.automations ?? [];
  return <div className="grid gap-3">
    <div className="flex items-baseline justify-between gap-3">
      <h2 className="font-semibold">Automations</h2>
      <button type="button" className="text-ui text-muted-foreground underline" onClick={onOlder}>Older automations</button>
    </div>
    {data && rows.length > 0 && <p className="text-ui text-muted-foreground">To add one, ask @lightbulb in any chat (&ldquo;every Monday at 9, …&rdquo;).</p>}
    {!data && <p role="status" className="text-ui text-muted-foreground">{error ? "Automations aren't available yet." : "Loading…"}</p>}
    {data && rows.length === 0 && <p className="rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      Ask @lightbulb to put recurring work on a schedule.
    </p>}
    {rows.length > 0 && <ul className="grid">{rows.map((a) => <li key={a.id} className="relative flex items-center gap-3 rounded-md px-2 py-2 hover:bg-accent">
      <div className="min-w-0 flex-1">
        <button type="button" className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:content-['']" onClick={() => onSelect(a.id)}>{a.name}</button>
        <p className="truncate text-ui text-muted-foreground">{[automationRowLine(a, viewerTz), deliveryWords(a.deliverTo)].filter(Boolean).join(" · ")}</p>
      </div>
      <RunNowButton a={a} onOpenTask={onOpenTask} onNote={setNote} />
    </li>)}</ul>}
    {note && <p role="status" className="text-ui text-muted-foreground">{note}</p>}
  </div>;
}

function AutomationDetail({ id, viewerTz, onBack, onOpenTask }: {
  id: string; viewerTz: string; onBack: () => void; onOpenTask: (taskId: string) => void;
}): ReactElement {
  const source = useAutomationSource();
  const specialistName = useSpecialistLabel();
  const { data: a, error } = useAutomation(source, id);
  const { data: runs } = useAutomationRuns(source, id);
  const { act, edit, remove } = useAutomationActions();
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const canDecide = useCanDecide();
  const canEdit = useCanEdit();
  const { userId } = useAuth();
  const [note, setNote] = useState<string | null>(null);
  const press = async (verb: "pause" | "resume") => {
    try { await act(id, verb); setNote(null); }
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
      <p className="text-ui text-muted-foreground">{[scheduleLine(a, viewerTz), specialistName(a.specialistId), budgetLine(a)].filter(Boolean).join(" · ")}</p>
      {automationStatusLine(a) && <p className="text-ui text-warn">{automationStatusLine(a)}</p>}
    </header>
    {/* Pause and Run now follow the server's _may_edit (admin or owner); Resume is an activate (room → admin only). */}
    {canEdit(a) || canDecide(a)
      ? <div className="flex flex-wrap items-center gap-2">
        {a.status !== "draft" && !paused && canEdit(a) && <Button type="button" size="sm" variant="outline" onClick={() => void press("pause")}>Pause</Button>}
        {paused && canDecide(a) && <Button type="button" size="sm" variant="outline" onClick={() => void press("resume")}>Resume</Button>}
        {paused && !canDecide(a) && <span className="text-ui text-muted-foreground">{waitingWords(a)}</span>}
        {canEdit(a) && <RunNowButton a={a} onOpenTask={onOpenTask} onNote={setNote} />}
      </div>
      : <p className="text-ui text-muted-foreground">{waitingWords(a)}</p>}
    {note && <p role="status" className="text-ui text-muted-foreground">{note}</p>}
    {editing
      ? <EditAutomation a={a} viewerTz={viewerTz} onCancel={() => setEditing(false)} onSave={async (body) => {
        try { await edit(a.id, body, a.version); setEditing(false); setNote(null); }
        catch (error) { setNote(saveError(error)); }
      }} />
      : <section aria-label="Instructions" className="editorial-card px-3 py-2">
        <h3 className="label-caps">Instructions</h3>
        <p className="whitespace-pre-wrap text-ui">{a.instructions}</p>
      </section>}
    {canEdit(a) && !editing && <div className="flex flex-wrap items-center gap-2">
      <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(true)}>Edit</Button>
      {deleting
        ? <span role="group" aria-label={`Delete ${a.name}?`} className="inline-flex items-center gap-2 text-ui">
          <span>Delete this automation? Its past runs stay.</span>
          <Button type="button" size="sm" variant="outline" onClick={async () => {
            try { await remove(a.id); onBack(); } catch { setNote("That didn't go through. Try again."); setDeleting(false); }
          }}>Delete</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setDeleting(false)}>Keep it</Button>
        </span>
        : <Button type="button" size="sm" variant="ghost" onClick={() => setDeleting(true)}>Delete…</Button>}
    </div>}
    <section aria-label="History" className="grid gap-1">
      <h3 className="label-caps">History</h3>
      {runs?.skipped && <p className="px-2 text-ui text-muted-foreground">{runs.skipped.text}</p>}
      {!runs?.runs.length
        ? <p className="text-ui text-muted-foreground">No runs yet.</p>
        : <ul className="grid">{runs.runs.map((t) => <li key={t.taskId}>
          <button type="button" className="flex w-full items-center gap-3 rounded-md px-2 py-1.5 text-left hover:bg-accent" onClick={() => onOpenTask(t.taskId)}>
            <span className="min-w-0 flex-1 truncate">{t.title}</span>
            <span className="text-ui text-muted-foreground">{summaryView(t, userId ?? "").word}</span>
          </button>
        </li>)}</ul>}
    </section>
  </div>;
}

/** Instructions and the time of day (days and zone kept); a rule has no schedule to move. */
function EditAutomation({ a, viewerTz, onSave, onCancel }: {
  a: AutomationView; viewerTz: string; onSave: (body: { instructions?: string; schedule?: { cron: string[]; tz: string; remainingRuns?: number | null } }) => Promise<void>; onCancel: () => void;
}): ReactElement {
  const [instructions, setInstructions] = useState(a.instructions);
  // The time is editable only for one cron entry ("Mondays 9:00"); "9:00 and 17:00" is edited by asking Lightbulb.
  const start = a.schedule?.cron.length === 1 ? cronTime(a.schedule.cron) : null;
  const [time, setTime] = useState(start ?? "");
  const [saving, setSaving] = useState(false);
  const zone = a.schedule && a.schedule.tz !== viewerTz ? ` (${a.schedule.tz.split("/").pop()?.replace(/_/g, " ")} time)` : "";
  return <form aria-label="Edit automation" className="editorial-card grid gap-2 px-3 py-2" onSubmit={async (event) => {
    event.preventDefault();
    setSaving(true);
    await onSave({
      ...(instructions.trim() !== a.instructions ? { instructions: instructions.trim() } : {}),
      ...(a.schedule && start && time && time !== start ? { schedule: { cron: withCronTime(a.schedule.cron, time), tz: a.schedule.tz, remainingRuns: a.schedule.remainingRuns ?? null } } : {}),
    });
    setSaving(false);
  }}>
    <label className="grid gap-1 text-ui"><span className="label-caps">Instructions</span>
      <textarea value={instructions} onChange={(event) => setInstructions(event.target.value)} rows={5} maxLength={20000} required
        className="w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-ui" />
    </label>
    {/* routers/automations.py: an edited rule's instructions make it a new rule, back in observe. */}
    {a.match && <p className="text-ui text-muted-foreground">Saving puts this rule back to watch-only until it's turned on again.</p>}
    {start && <label className="grid w-fit gap-1 text-ui"><span className="label-caps">Time{zone}</span>
      <input type="time" value={time} onChange={(event) => setTime(event.target.value)} required
        className="rounded-md border border-input bg-background px-2 py-1 text-ui" />
    </label>}
    <div className="flex gap-2">
      <Button type="submit" size="sm" disabled={saving || !instructions.trim()}>Save</Button>
      <Button type="button" size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>
    </div>
  </form>;
}

/** A failed save in words: a 409 is a newer version, a 400 a schedule or text the server refused. */
function saveError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) return "Someone changed this automation. Reopen it and try again.";
  if (error instanceof ApiError && error.status === 400) return "That schedule or text wasn't accepted. Check it and try again.";
  return "That didn't save. Try again.";
}
