"use client";

import { AutomationDetail } from "@/components/tasks/AutomationDetail";
import { viewerTimeZone } from "@/components/tasks/automations";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { closePane, getWorkspaceSnapshot, openTaskPane } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

/** A project's pane listens for this: "‹ Scheduled" from an automation goes back to its list (`view: "scheduled"`),
 *  and its ✕ closes the project's pane too (`view: null`), so one ✕ leaves nothing behind. */
export const ROOM_PANE_VIEW_EVENT = "lightbulb:room-pane-view";

/** Back from a pane item to where it was opened (`back`: "tasks", "files", "scheduled", "task:<id>", or null for
 *  nowhere). With a project as the place, its pane takes that view (or that task) again; elsewhere a task reopens in
 *  the pane, and a list, being beside it, just closes. */
export function leavePane(back: string | null): void {
  const inRoom = getWorkspaceSnapshot().place === "chat";
  const taskId = back?.startsWith("task:") ? back.slice(5) : null;
  closePane();
  if (inRoom) window.dispatchEvent(new CustomEvent(ROOM_PANE_VIEW_EVENT, { detail: taskId ? { view: "task", taskId } : { view: back } }));
  else if (taskId) openTaskPane(taskId);
}

/** One automation in the pane (`?pane=automation&pane.id=`). "‹ Scheduled" returns to the project's Scheduled view
 *  when a project is the place; elsewhere (Activity's Scheduled group) it closes the pane, the list being beside it.
 *  Off the switch it renders nothing, like the task pane. */
export function AutomationDockPanel({ params }: PanelProps) {
  const on = useSuperAgent();
  if (!on) return null;
  return <AutomationDetail key={params.id} id={params.id} viewerTz={viewerTimeZone()} onBack={() => leavePane("scheduled")}
    onClose={() => leavePane(null)} onOpenTask={openTaskPane} />;
}
