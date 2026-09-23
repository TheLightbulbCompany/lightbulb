"use client";

// A room's Tasks tab: the tasks its messages have named, grouped by what they
// need. Same derivation and rows as the inline card (MessageTaskCard).

import type { Issue } from "@/components/teams/shared/types";
import { isTerminalStatus } from "@/components/teams/shared/lib/subIssues";
import { needsYouAction, TaskRowList } from "./MessageTaskCard";

const GROUPS = ["Needs you", "In progress", "Up next", "Done"] as const;

function group(issue: Issue): (typeof GROUPS)[number] {
  if (needsYouAction(issue)) return "Needs you";
  if (isTerminalStatus(issue.status)) return "Done";
  if (issue.status === "todo" || issue.status === "backlog" || issue.status === "open") return "Up next";
  return "In progress";
}

export function RoomTasks({ tasks, onOpenAll }: { tasks: readonly Issue[]; onOpenAll: () => void }) {
  return <>
    {tasks.length === 0 ? <p className="text-sm text-muted-foreground">No tasks mentioned in this room yet.</p>
      : GROUPS.map((name) => {
        const issues = tasks.filter((issue) => group(issue) === name);
        return issues.length > 0 && <section key={name}>
          <h4 className="font-semibold">{name}</h4>
          <TaskRowList issues={issues} label={name} />
        </section>;
      })}
    <button type="button" className="mt-4 block text-sm text-[var(--midnight-muted)] hover:underline" onClick={onOpenAll}>
      All tasks in Tasks <span aria-hidden="true">→</span>
    </button>
  </>;
}
