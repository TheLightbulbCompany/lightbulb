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
  columns?: readonly string[];
  onStatusChange?: (id: string, status: string) => void;
}

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function KanbanColumn({
  status,
  issues,
  agents,
  liveIssueIds,
  columns,
  onStatusChange,
}: KanbanColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: status });

  // Every status column is always full-width with its label visible — empty
  // workflow stages must not vanish (they're still valid drop targets, and
  // hiding their names made the board unreadable). `flex-1` lets the columns
  // grow to fill the board width (no dead space on the right); `min-w-[260px]`
  // keeps them readable and lets the board scroll horizontally when narrow.
  return (
    <div
      data-kanban-column={status}
      className="flex h-full min-w-[260px] flex-1 flex-col"
    >
      {/* Header stays put above the scrolling card list, so column names +
          counts remain visible while you scroll a tall column. */}
      <div className="mb-1 flex shrink-0 items-center gap-2 px-2 py-2">
        <StatusIcon status={status} />
        <span
          className="label-caps"
          style={{ color: "var(--midnight-muted)" }}
        >
          {statusLabel(status)}
        </span>
        <span
          className="text-caption ml-auto tabular-nums"
          style={{ color: "var(--midnight-muted)" }}
        >
          {issues.length}
        </span>
      </div>
      <div
        ref={setNodeRef}
        className={cn(
          "min-h-0 flex-1 space-y-1 overflow-y-auto rounded-md p-1 transition-colors",
        )}
        style={{
          backgroundColor: isOver ? "var(--edge)" : "var(--cream-deep)",
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
              columns={columns}
              onStatusChange={onStatusChange}
            />
          ))}
        </SortableContext>
      </div>
    </div>
  );
}
