"use client";

import { useAuth } from "@clerk/nextjs";
import { TaskPane } from "@/components/tasks/TaskPane";
import { closePane } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

/** The right pane for one thread, opened from Activity, a notification or a link. */
export function TaskDockPanel({ params }: PanelProps) {
  const { userId } = useAuth();
  // TaskPane is a `.room-context` column, hidden (display: none) outside a `.room-context-host` (messaging.css):
  // the host makes it fill the pane instead of rendering blank.
  return <div className="room-context-host flex h-full min-h-0">
    <TaskPane key={params.taskId} taskId={params.taskId} viewerId={userId ?? ""} onClose={closePane} />
  </div>;
}
