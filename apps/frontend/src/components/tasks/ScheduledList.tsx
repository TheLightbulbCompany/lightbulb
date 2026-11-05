"use client";

// The overview's Scheduled tab: the workspace's automations (lane A, `GET /api/v1/automations`).

import type { ReactElement } from "react";
import { automationStatusLine, useAutomations, type AutomationView, type TaskScope } from "@lightbulb/shared";
import { useAutomationSource } from "./automations";
import { openPanel } from "@/components/workspace/workspaceController";

export function ScheduledList({ scope, onOpen, onOpenAutomation }: {
  scope: TaskScope; onOpen: (taskId: string) => void; onOpenAutomation?: (id: string) => void;
}): ReactElement {
  const { data, error, mutate } = useAutomations(useAutomationSource());
  // A room's tab keeps the automations that deliver there; the workspace's and a person's show all.
  const rows = (data?.automations ?? []).filter((a) => scope.kind !== "room" || !a.deliverTo || a.deliverTo.includes(scope.roomId));
  if (!data) {
    return error
      ? <p role="alert" className="text-ui text-muted-foreground">
        Schedules couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button>
      </p>
      : <p role="status" className="text-ui text-muted-foreground">Loading…</p>;
  }
  // A row opens its run (live, else last); one that never ran opens Automations, where it is listed with its controls.
  const open = (a: AutomationView) => {
    const t = a.activeRunTaskId ?? a.lastTaskId;
    if (onOpenAutomation) onOpenAutomation(a.id);
    else if (t) onOpen(t);
    else openPanel("teams-routines", {}, { pushUrl: true });
  };
  if (!rows.length) {
    return <p className="rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      Ask @lightbulb to put recurring work on a schedule.
    </p>;
  }
  return <ul className="grid">{rows.map((a) => <li key={a.id} className="relative flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent">
    <div className="min-w-0 flex-1">
      <button type="button" className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:content-['']"
        onClick={() => open(a)}>{a.name}</button>
      {automationStatusLine(a) && <p className="truncate text-ui text-muted-foreground">{automationStatusLine(a)}</p>}
    </div>
  </li>)}</ul>;
}
