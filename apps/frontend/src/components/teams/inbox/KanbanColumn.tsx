// Ported from upstream Paperclip's KanbanBoard inner column
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// Editorial retheme: bg-muted/20 → paper-soft, accent → ink-tint.

"use client";

import { useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { cn } from "@/lib/utils";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { KanbanCard, type KanbanAgent } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

export interface KanbanColumnProps {
  status: string;
  issues: Issue[];
  agents?: KanbanAgent[];
  liveIssueIds?: Set<string>;
}

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function KanbanColumn({
  status,
  issues,
  agents,
  liveIssueIds,
}: KanbanColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: status });
  const isEmpty = issues.length === 0;

  return (
    <div
      data-kanban-column={status}
      data-empty={isEmpty ? "true" : "false"}
      className={cn(
        "flex flex-col shrink-0 transition-[width,min-width]",
        isEmpty && !isOver
          ? "min-w-[48px] w-[48px]"
          : "min-w-[260px] w-[260px]",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-2 px-2 py-2 mb-1",
          isEmpty && !isOver && "justify-center",
        )}
      >
        <StatusIcon status={status} />
        {(!isEmpty || isOver) && (
          <>
            <span
              className="text-xs font-semibold uppercase tracking-wide"
              style={{ color: "var(--mute)" }}
            >
              {statusLabel(status)}
            </span>
            <span
              className="text-xs ml-auto tabular-nums"
              style={{ color: "var(--mute-soft)" }}
            >
              {issues.length}
            </span>
          </>
        )}
      </div>
      <div
        ref={setNodeRef}
        className={cn(
          "flex-1 min-h-[120px] rounded-md p-1 space-y-1 transition-colors",
        )}
        style={{
          backgroundColor: isOver ? "var(--paper)" : "var(--paper-soft)",
        }}
      >
        <SortableContext
          items={issues.map((i) => i.id)}
          strategy={verticalListSortingStrategy}
        >
          {issues.map((issue) => (
            <KanbanCard
              key={issue.id}
              issue={issue}
              agents={agents}
              isLive={liveIssueIds?.has(issue.id)}
            />
          ))}
        </SortableContext>
      </div>
    </div>
  );
}
