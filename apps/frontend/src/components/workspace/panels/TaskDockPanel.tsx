"use client";

import { useAuth } from "@clerk/nextjs";
import { TaskPane } from "@/components/tasks/TaskPane";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { closePane } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

/** The right pane for one thread, opened from Activity, a notification or a link.
 *  Off the switch it renders nothing: a pasted `?panel=task-detail` link must not reach `/tasks`. */
export function TaskDockPanel({ params }: PanelProps) {
  const on = useSuperAgent();
  const { userId } = useAuth();
  if (!on) return null;
  return <TaskPane key={params.taskId} taskId={params.taskId} viewerId={userId ?? ""} onClose={closePane} />;
}
