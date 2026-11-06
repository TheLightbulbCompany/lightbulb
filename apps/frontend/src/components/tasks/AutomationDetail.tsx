"use client";

// One automation (super-agent): its schedule in the viewer's time zone, its instructions, Pause / Resume and Run now,
// and a history in which every run is its own task. It opens in the pane (`?pane=automation&pane.id=`), from a
// project's Scheduled view or Activity's Scheduled group, so a reload restores it.

import { useState, type ReactElement } from "react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, automationStatusLine, summaryView, useAutomation, useAutomationRuns, type AutomationView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { PaneHeader } from "@/components/workspace/PaneHeader";
import {
  budgetLine, cronTime, runNow, runNowReason, scheduleWords, useAutomationActions, useAutomationSource, useCanDecide, useCanEdit, useDeliveryWords, useSpecialistLabel, waitingWords, withCronTime,
} from "./automations";

const scheduleLine = (a: AutomationView, viewerTz: string): string => scheduleWords(a, viewerTz) ?? "No schedule";

/** Run now, or "Running ›" while it is On and a run is going (opens it); a paused row never reads Running. A rule (lane S) has no Run now: it fires on a match.
 *  When it can't run, the reason takes the button's place, never a dead Run now (UX slice 3): a paused one offers its
 *  one action, Resume, to whoever may resume it. */
function RunNowButton({ a, onOpenTask, onNote }: { a: AutomationView; onOpenTask: (taskId: string) => void; onNote: (note: string | null) => void }): ReactElement | null {
  const { act } = useAutomationActions();
  const canEdit = useCanEdit();
  const canDecide = useCanDecide();
  const [resuming, setResuming] = useState(false);  // one press: a double click resumes once
  if (a.match) return null;
  if (a.activeRunTaskId && a.status === "active") {
    return <Button type="button" size="sm" variant="ghost" className="relative z-10" onClick={() => onOpenTask(a.activeRunTaskId!)}>Running ›</Button>;
  }
  const paused = a.status === "paused" || a.status === "paused_system";
  if (paused && canDecide(a)) {
    return <Button type="button" size="sm" variant="outline" className="relative z-10" disabled={resuming}
      onClick={() => {
        if (resuming) return;
        setResuming(true);
        act(a.id, "resume").then(() => onNote(null), () => onNote("That didn't go through. Try again.")).finally(() => setResuming(false));
      }}>Resume</Button>;
  }
  const reason = runNowReason(a, canEdit(a));
  if (reason) return <span className="relative z-10 text-caption text-muted-foreground">{paused ? `${waitingWords(a)} to resume it` : reason}</span>;
  return <span className="relative z-10 inline-flex items-center gap-2">
    <Button type="button" size="sm" variant="outline"
      onClick={async () => {
        const res = await runNow(act, a);
        if (res.kind === "running") onOpenTask(res.taskId);
        onNote(res.kind === "started" ? `${a.name} is running.` : res.kind === "note" ? res.message : null);
      }}>Run now</Button>
  </span>;
}

export function AutomationDetail({ id, viewerTz, onBack, onOpenTask, onDeleted = onBack, onClose }: {
  id: string; viewerTz: string; onBack: () => void; onOpenTask: (taskId: string) => void; onDeleted?: () => void;
  /** ✕ (the pane's close by default). */
  onClose?: () => void;
}): ReactElement {
  const source = useAutomationSource();
  const specialistName = useSpecialistLabel();
  const delivery = useDeliveryWords();
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
  const crumb = { label: "Scheduled", onBack };
  if (!a) return <div className="flex min-h-0 flex-1 flex-col">
    <PaneHeader crumb={crumb} title="Scheduled" onClose={onClose} />
    <p role="status" className="p-4 text-ui text-muted-foreground">{error ? "This automation isn't available." : "Loading…"}</p>
  </div>;
  const paused = a.status === "paused" || a.status === "paused_system";
  return <div className="flex min-h-0 flex-1 flex-col">
    <PaneHeader crumb={crumb} title={a.name} meta={paused ? "Paused" : a.status === "draft" ? "Draft" : "On"} onClose={onClose} />
    <div className="grid min-h-0 flex-1 content-start gap-3 overflow-y-auto p-4">
    <header className="grid gap-1">
      <p className="text-ui text-muted-foreground">{[scheduleLine(a, viewerTz), delivery(a.deliverTo), specialistName(a.specialistId), budgetLine(a)].filter(Boolean).join(" · ")}</p>
      {automationStatusLine(a) && <p className="text-ui text-warn">{automationStatusLine(a)}</p>}
    </header>
    {/* Pause and Run now follow the server's _may_edit (admin or owner); Resume is an activate (room → admin only). */}
    {canEdit(a) || canDecide(a)
      ? <div className="flex flex-wrap items-center gap-2">
        {a.status !== "draft" && !paused && canEdit(a) && <Button type="button" size="sm" variant="outline" onClick={() => void press("pause")}>Pause</Button>}
        {paused && canDecide(a) && <Button type="button" size="sm" variant="outline" onClick={() => void press("resume")}>Resume</Button>}
        {paused && !canDecide(a) && <span className="text-ui text-muted-foreground">{waitingWords(a)}</span>}
        {canEdit(a) && !paused && <RunNowButton a={a} onOpenTask={onOpenTask} onNote={setNote} />}
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
            try { await remove(a.id); onDeleted(); } catch { setNote("That didn't go through. Try again."); setDeleting(false); }
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
    </div>
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
    {a.match && <p className="text-ui text-muted-foreground">Saving puts this rule back to watch-only until it&apos;s turned on again.</p>}
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
