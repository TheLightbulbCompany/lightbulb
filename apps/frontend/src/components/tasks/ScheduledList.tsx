"use client";

// The overview's Scheduled tab: the workspace's automations (lane A, `GET /api/v1/automations`).

import type { ReactElement } from "react";
import { Clock } from "lucide-react";
import { automationRowLine, useAutomations, viewerTimeZone, type AutomationView, type TaskScope } from "@lightbulb/shared";
import { useAutomationSource } from "./automations";
import { openPanel } from "@/components/workspace/workspaceController";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";
import { goalTemplates, pickTemplates } from "./scheduleTemplates";

export function ScheduledList({ scope, onOpen, onOpenAutomation, onDraft, goal, viewerTz = viewerTimeZone() }: {
  scope: TaskScope; onOpen: (taskId: string) => void; onOpenAutomation?: (id: string) => void; viewerTz?: string;
  /** The project's goal: its starter cards are built from it. */
  goal?: string | null;
  /** A project's empty view: a starter card drafts its ask in the project's composer (UX slice 6). */
  onDraft?: (text: string) => void;
}): ReactElement {
  const { data, error, mutate } = useAutomations(useAutomationSource());
  const roster = useOptionalLiveEvents()?.agentNames;
  // A room's tab keeps the automations that deliver there; the workspace's and a person's show all. The builtin jobs
  // live in Settings › Workspace, never in a project.
  const rows = (data?.automations ?? []).filter((a) => scope.kind !== "room"
    || (!a.id.startsWith("builtin:") && (!a.deliverTo || a.deliverTo.includes(scope.roomId))));
  if (!data) {
    return error
      ? <p role="alert" className="text-ui text-muted-foreground">
        Schedules couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button>
      </p>
      : <p role="status" className="text-ui text-muted-foreground">Loading…</p>;
  }
  // A row opens its run (live, else last); one that never ran opens Activity's Scheduled group, where it is listed with its controls.
  const open = (a: AutomationView) => {
    const t = a.activeRunTaskId ?? a.lastTaskId;
    if (onOpenAutomation) onOpenAutomation(a.id);
    else if (t) onOpen(t);
    else openPanel("activity", { section: "scheduled" }, { pushUrl: true });
  };
  if (!rows.length && onDraft) {
    const fromGoal = goalTemplates(goal);
    return <div className="grid gap-2 pt-4">
      <Clock size={20} aria-hidden="true" className="justify-self-center text-muted-foreground" />
      <div className="text-center">
        <p>Put recurring work on a schedule.</p>
        <p className="text-caption text-muted-foreground">Start from one of these, or ask in the chat.</p>
      </div>
      <ul className="mt-2 grid gap-2" aria-label="Starter schedules">
        {(fromGoal.length ? fromGoal : pickTemplates(roster?.values() ?? [])).map((t) => <li key={t.name}>
          <button type="button" className="flex min-h-16 w-full items-start gap-2 rounded-lg border border-border px-3 py-2.5 text-left transition-colors duration-(--dur-fast) hover:bg-accent"
            onClick={() => onDraft(t.draft)}>
            <Clock size={14} aria-hidden="true" className="mt-1 flex-none text-muted-foreground" />
            <span className="grid min-w-0 flex-1">
              <span className="flex items-baseline gap-2"><span className="min-w-0 flex-1 truncate font-medium">{t.name}</span>
                <span className="flex-none text-caption text-muted-foreground">{t.specialist}</span></span>
              <span className="text-caption text-muted-foreground">{t.cadence} · posts here</span>
            </span>
          </button>
        </li>)}
      </ul>
    </div>;
  }
  if (!rows.length) {
    return <p className="rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      Ask @lightbulb to put recurring work on a schedule.
    </p>;
  }
  return <ul className="grid">{rows.map((a) => <li key={a.id} className="relative flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent">
    <div className="min-w-0 flex-1">
      <button type="button" className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:content-['']"
        onClick={() => open(a)}>{a.name}</button>
      <p className="truncate text-ui text-muted-foreground">{automationRowLine(a, viewerTz)}</p>
    </div>
  </li>)}</ul>;
}
