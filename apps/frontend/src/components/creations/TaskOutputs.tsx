"use client";

// One chip per thing a task made (super-agent, UX slice 5), on its card in chat and in its pane: `[▣ Walk7 Signup ›]`.
// A chip opens the output; past `max`, "+N more" opens the task.

import "./creations.css";
import type { ReactElement } from "react";
import { ChevronRight } from "lucide-react";
import type { TaskSummary } from "@lightbulb/shared";
import { openOutput } from "./openOutput";
import { KIND_ICON } from "./OutputThumb";

export function TaskOutputs({ task, max, onOpenTask }: {
  task: Pick<TaskSummary, "taskId" | "outputs">;
  /** Chips shown before "+N more"; all of them when absent. */
  max?: number;
  /** Opens the task: "+N more", and mail or a capability (whose record is the task). Absent (the task is already
   *  open) those chips are labels. */
  onOpenTask?: () => void;
}): ReactElement | null {
  const outputs = task.outputs ?? [];
  if (!outputs.length) return null;
  const shown = max === undefined ? outputs : outputs.slice(0, max);
  const rest = outputs.length - shown.length;
  return <ul className="task-outputs" aria-label="Made by this task">
    {shown.map((o) => {
      const Icon = KIND_ICON[o.kind];
      const opensTask = o.kind === "mail" || o.kind === "capability";
      const body = <><Icon size={14} strokeWidth={1.75} aria-hidden="true" /><span className="task-outputs__title">{o.title}</span></>;
      return <li key={o.id}>
        {opensTask && !onOpenTask ? <span className="task-outputs__chip">{body}</span>
          : <button type="button" className="task-outputs__chip" aria-label={`Open ${o.title}`}
            onClick={(e) => { e.stopPropagation(); openOutput({ ...o, taskId: task.taskId }, onOpenTask ? () => onOpenTask() : undefined); }}>
            {body}<ChevronRight size={14} aria-hidden="true" className="task-outputs__chev" />
          </button>}
      </li>;
    })}
    {rest > 0 && onOpenTask && <li><button type="button" className="task-outputs__more" onClick={(e) => { e.stopPropagation(); onOpenTask(); }}>+{rest} more</button></li>}
  </ul>;
}
