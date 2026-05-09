// apps/frontend/src/components/teams/inbox/KanbanBoard.tsx

// Ported from upstream Paperclip's KanbanBoard
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// 7-column board with drag-to-change-status. Order-within-column is not
// persisted; drag-end only patches `status`.

"use client";

import { useMemo, useState } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { KanbanColumn } from "@/components/teams/inbox/KanbanColumn";
import { KanbanCardOverlay, type KanbanAgent } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

export const BOARD_STATUSES = [
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "blocked",
  "done",
  "cancelled",
] as const;

export type BoardStatus = (typeof BOARD_STATUSES)[number];

export interface KanbanBoardProps {
  issues: Issue[];
  agents?: KanbanAgent[];
  liveIssueIds?: Set<string>;
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void;
}

// Exported for unit tests — pure logic that doesn't require a rendered
// DndContext to verify drag-end semantics.
export function __test_handleDragEnd(
  issues: Issue[],
  onUpdateIssue: KanbanBoardProps["onUpdateIssue"],
  event: DragEndEvent,
): void {
  const { active, over } = event;
  if (!over) return;
  const issueId = active.id as string;
  const issue = issues.find((i) => i.id === issueId);
  if (!issue) return;

  let targetStatus: string | null = null;
  if ((BOARD_STATUSES as readonly string[]).includes(over.id as string)) {
    targetStatus = over.id as string;
  } else {
    const targetIssue = issues.find((i) => i.id === over.id);
    if (targetIssue) targetStatus = targetIssue.status;
  }

  if (targetStatus && targetStatus !== issue.status) {
    onUpdateIssue(issueId, { status: targetStatus });
  }
}

export function KanbanBoard({
  issues,
  agents,
  liveIssueIds,
  onUpdateIssue,
}: KanbanBoardProps) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const columnIssues = useMemo(() => {
    const grouped: Record<string, Issue[]> = {};
    for (const status of BOARD_STATUSES) grouped[status] = [];
    for (const issue of issues) {
      if (grouped[issue.status]) grouped[issue.status].push(issue);
    }
    return grouped;
  }, [issues]);

  const activeIssue = useMemo(
    () => (activeId ? issues.find((i) => i.id === activeId) ?? null : null),
    [activeId, issues],
  );

  function handleDragStart(event: DragStartEvent) {
    setActiveId(event.active.id as string);
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveId(null);
    __test_handleDragEnd(issues, onUpdateIssue, event);
  }

  return (
    <DndContext
      sensors={sensors}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      <div className="flex gap-3 overflow-x-auto pb-4 -mx-2 px-2">
        {BOARD_STATUSES.map((status) => (
          <KanbanColumn
            key={status}
            status={status}
            issues={columnIssues[status] ?? []}
            agents={agents}
            liveIssueIds={liveIssueIds}
          />
        ))}
      </div>
      <DragOverlay>
        {activeIssue ? (
          <KanbanCardOverlay issue={activeIssue} agents={agents} />
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}
