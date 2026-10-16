// apps/frontend/src/components/teams/inbox/KanbanBoard.tsx

// Ported from upstream Paperclip's KanbanBoard
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// 7-column board with drag-to-change-status. Order-within-column is not
// persisted; drag-end only patches `status`. The move is optimistic: the host's
// `onUpdateIssue` writes the new status into its cached list before the PATCH
// returns (see `updateIssueInList`), so the card stays where it was dropped.

"use client";

import { useMemo, useState } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import { KanbanColumn } from "@/components/teams/inbox/KanbanColumn";
import { KanbanCardOverlay, type KanbanAgent } from "@/components/teams/inbox/KanbanCard";
import { notifyApiError } from "@/lib/notify";
import type { CompanyMember, Issue } from "@/components/teams/shared/types";

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
  members?: CompanyMember[];
  liveIssueIds?: Set<string>;
  /** A returned promise that rejects means the move failed: the host has
   * already rolled its cache back, and the board says so in a toast. */
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void | Promise<unknown>;
}

/**
 * The columns to render: the seven canonical workflow stages, always, plus a
 * column for any other status actually present on the issues.
 *
 * The board used to render `BOARD_STATUSES` alone and drop everything else on
 * the floor (`if (grouped[issue.status])`), which made a task with a status
 * outside those seven **invisible**. That is not hypothetical: `ISSUE_STATUSES`
 * — which `searchQueryParser` calls the board-column set — carries `pending`,
 * `review`, `won_t_do` and `duplicate`, none of which are in `BOARD_STATUSES`,
 * and `IssueStatus` additionally allows `open` and `closed`. The two sets
 * diverge in both directions.
 *
 * Deriving the extras from the data rather than widening the constant means the
 * next enum drift surfaces as a visible column on day one instead of silently
 * hiding rows — the same call `rosterStateFor` makes for an unrecognised agent
 * status. The canonical seven still render when empty, because an empty
 * workflow stage is information and a valid drop target.
 */
export function boardColumns(issues: Issue[]): string[] {
  const known = new Set<string>(BOARD_STATUSES);
  const extra = new Set<string>();
  for (const issue of issues) {
    if (!known.has(issue.status)) extra.add(issue.status);
  }
  return [...BOARD_STATUSES, ...[...extra].sort()];
}

// Exported for unit tests — pure logic that doesn't require a rendered
// DndContext to verify drag-end semantics.
export function __test_handleDragEnd(
  issues: Issue[],
  onUpdateIssue: KanbanBoardProps["onUpdateIssue"],
  event: DragEndEvent,
  // Every rendered column is a drop target, not just the canonical seven —
  // otherwise a card could be dragged OUT of a derived column but never back
  // INTO it, which reads as a broken column rather than a deliberate one.
  columns: readonly string[] = BOARD_STATUSES,
): void {
  const { active, over } = event;
  if (!over) return;
  const issueId = active.id as string;
  const issue = issues.find((i) => i.id === issueId);
  if (!issue) return;

  let targetStatus: string | null = null;
  if (columns.includes(over.id as string)) {
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
  members,
  liveIssueIds,
  onUpdateIssue,
}: KanbanBoardProps) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const columns = useMemo(() => boardColumns(issues), [issues]);

  const columnIssues = useMemo(() => {
    const grouped: Record<string, Issue[]> = {};
    for (const status of columns) grouped[status] = [];
    for (const issue of issues) grouped[issue.status]?.push(issue);
    return grouped;
  }, [issues, columns]);

  const activeIssue = useMemo(
    () => (activeId ? issues.find((i) => i.id === activeId) ?? null : null),
    [activeId, issues],
  );

  function handleDragStart(event: DragStartEvent) {
    setActiveId(event.active.id as string);
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveId(null);
    __test_handleDragEnd(
      issues,
      (id, data) =>
        void Promise.resolve(onUpdateIssue(id, data)).catch((e) =>
          notifyApiError(e, "Couldn't move the task. Try again."),
        ),
      event,
      columns,
    );
  }

  return (
    <DndContext
      sensors={sensors}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      <div className="flex h-full min-h-0 gap-3 overflow-x-auto pb-1 -mx-2 px-2">
        {columns.map((status) => (
          <KanbanColumn
            key={status}
            status={status}
            issues={columnIssues[status] ?? []}
            agents={agents}
            members={members}
            liveIssueIds={liveIssueIds}
          />
        ))}
      </div>
      {/* No drop animation: the default flies the overlay back to the card's
          pre-drop slot, which read as the card snapping back before landing. The
          optimistic move already renders it in the target column. */}
      <DragOverlay dropAnimation={null}>
        {activeIssue ? (
          <KanbanCardOverlay issue={activeIssue} agents={agents} members={members} />
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}
