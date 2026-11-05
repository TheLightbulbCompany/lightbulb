"use client";

// The overview's Scheduled tab: the workspace's automations (lane A, `GET /api/v1/automations`).
// Until those routes are on, the read fails and this shows the empty state.

import type { ReactElement } from "react";
import useSWR from "swr";
import type { TaskScope } from "@lightbulb/shared";
import { useApi } from "@/lib/api";

export interface AutomationRow {
  id: string;
  name: string;
  status: "draft" | "active" | "paused" | "paused_system";
  pausedReason?: string | null;
  schedule?: { cron: string[]; tz: string } | null;
  roomId?: string | null;
  lastTaskId?: string | null;
}

export function useAutomations(enabled = true) {
  const api = useApi();
  return useSWR<{ automations: AutomationRow[] }>(enabled ? "automations" : null, () => api.get("/automations") as Promise<{ automations: AutomationRow[] }>,
    { shouldRetryOnError: false });
}

export function statusLine(a: AutomationRow): string | null {
  if (a.status === "paused" || a.status === "paused_system") return a.pausedReason ? `Paused: ${a.pausedReason}` : "Paused";
  if (a.status === "draft") return "Not turned on";
  return null;
}

export function ScheduledList({ scope, onOpen, onOpenAutomation }: {
  scope: TaskScope; onOpen: (taskId: string) => void; onOpenAutomation?: (id: string) => void;
}): ReactElement {
  const { data } = useAutomations();
  const rows = (data?.automations ?? []).filter((a) => scope.kind !== "room" || !a.roomId || a.roomId === scope.roomId);
  if (!rows.length) {
    return <p className="rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground">
      Ask @lightbulb to put recurring work on a schedule.
    </p>;
  }
  return <ul className="grid">{rows.map((a) => <li key={a.id} className="relative flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-accent">
    <div className="min-w-0 flex-1">
      <button type="button" className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:content-['']"
        onClick={() => (onOpenAutomation ? onOpenAutomation(a.id) : a.lastTaskId && onOpen(a.lastTaskId))}>{a.name}</button>
      {statusLine(a) && <p className="truncate text-ui text-muted-foreground">{statusLine(a)}</p>}
    </div>
  </li>)}</ul>;
}
