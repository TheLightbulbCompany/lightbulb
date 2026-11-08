"use client";

// One chip per thing a task made (super-agent, UX slice 5), on its card in chat and in its pane: `[▣ Walk7 Signup ›]`.
// A chip opens the output; past `max`, "+N more" opens the task.

import "./creations.css";
import { useContext, type ReactElement } from "react";
import { ChevronRight } from "lucide-react";
import type { TaskSummary } from "@lightbulb/shared";
import { OutputBackContext, extensionOf, openOutput, outputName } from "./openOutput";
import { iconOf } from "./OutputThumb";

export function TaskOutputs({ task, max, onOpenTask }: {
  task: Pick<TaskSummary, "taskId" | "outputs">;
  /** Chips shown before "+N more"; all of them when absent. */
  max?: number;
  /** Opens the task: "+N more", and mail or a capability (whose record is the task). Absent (the task is already
   *  open) those chips are labels. */
  onOpenTask?: () => void;
}): ReactElement | null {
  // A short text result (`text:` id) is shown whole where these chips sit (the finish message, the task): no chip.
  const outputs = (task.outputs ?? []).filter((o) => !o.id.startsWith("text:"));
  // In the open task its file goes back to the task ("‹ Task"); on its chip in chat, back to the chat (W16-2).
  const back = useContext(OutputBackContext) ?? "chat";
  if (!outputs.length) return null;
  const shown = max === undefined ? outputs : outputs.slice(0, max);
  const rest = outputs.length - shown.length;
  return <ul className="task-outputs" aria-label="Made by this thread">
    {shown.map((o) => {
      const opensTask = o.kind === "mail" || o.kind === "capability";
      // Its readable name, not the file's slug, and its type, so a PDF and a Word copy of one sheet differ (W18).
      const name = outputName(o);
      const ext = o.kind === "file" || o.kind === "document" ? extensionOf(o) : "";
      const full = ext ? `${name} · ${ext}` : name;
      const body = <>{iconOf(o)}<span className="task-outputs__title">{name}</span>
        {ext && <span className="task-outputs__ext">{ext}</span>}</>;
      return <li key={o.id}>
        {opensTask && !onOpenTask ? <span className="task-outputs__chip" title={full}>{body}</span>
          : <button type="button" className="task-outputs__chip" title={full} aria-label={`Open ${full}`}
            onClick={(e) => { e.stopPropagation(); openOutput({ ...o, taskId: task.taskId }, onOpenTask ? () => onOpenTask() : undefined, back); }}>
            {body}<ChevronRight size={14} aria-hidden="true" className="task-outputs__chev" />
          </button>}
      </li>;
    })}
    {rest > 0 && onOpenTask && <li><button type="button" className="task-outputs__more" onClick={(e) => { e.stopPropagation(); onOpenTask(); }}>+{rest} more</button></li>}
  </ul>;
}
