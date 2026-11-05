"use client";

import { useAuth } from "@clerk/nextjs";
import { TaskPane } from "@/components/tasks/TaskPane";
import { closePane } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

/** The right pane for one thread, opened from Activity, a notification or a link. */
export function TaskDockPanel({ params }: PanelProps) {
  const { userId } = useAuth();
  return <TaskPane key={params.taskId} taskId={params.taskId} viewerId={userId ?? ""} onClose={closePane} />;
}
