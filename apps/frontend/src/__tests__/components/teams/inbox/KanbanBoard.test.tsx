import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { KanbanBoard, BOARD_STATUSES, __test_handleDragEnd } from "@/components/teams/inbox/KanbanBoard";
import type { Issue } from "@/components/teams/shared/types";

const i = (id: string, status: Issue["status"]): Issue => ({
  id,
  identifier: id.toUpperCase(),
  title: `Issue ${id}`,
  status,
  priority: null,
});

describe("KanbanBoard", () => {
  it("groups issues into columns by status", () => {
    const issues = [i("a", "todo"), i("b", "in_progress"), i("c", "todo")];
    const { container } = render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);
    const todoCol = container.querySelector('[data-kanban-column="todo"]');
    const inProgCol = container.querySelector('[data-kanban-column="in_progress"]');
    expect(todoCol?.querySelectorAll("[data-kanban-card-id]")).toHaveLength(2);
    expect(inProgCol?.querySelectorAll("[data-kanban-card-id]")).toHaveLength(1);
  });

  it("renders all 7 board statuses as columns", () => {
    const { container } = render(<KanbanBoard issues={[]} agents={[]} onUpdateIssue={vi.fn()} />);
    for (const status of BOARD_STATUSES) {
      expect(container.querySelector(`[data-kanban-column="${status}"]`)).toBeInTheDocument();
    }
  });

  it("BOARD_STATUSES exposes the canonical 7-status set in upstream order", () => {
    expect(BOARD_STATUSES).toEqual([
      "backlog",
      "todo",
      "in_progress",
      "in_review",
      "blocked",
      "done",
      "cancelled",
    ]);
  });

  it("issues with non-board statuses do not render", () => {
    const issues = [i("a", "todo"), i("b", "pending" as Issue["status"])];
    const { container } = render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);
    expect(container.querySelector('[data-kanban-card-id="a"]')).toBeInTheDocument();
    expect(container.querySelector('[data-kanban-card-id="b"]')).not.toBeInTheDocument();
  });

  it("__test_handleDragEnd patches status when dropping on a column id", () => {
    const onUpdateIssue = vi.fn();
    const issues = [i("a", "todo")];
    __test_handleDragEnd(
      issues,
      onUpdateIssue,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { active: { id: "a" }, over: { id: "in_progress" } } as any,
    );
    expect(onUpdateIssue).toHaveBeenCalledWith("a", { status: "in_progress" });
  });

  it("__test_handleDragEnd is a no-op when dropping on the same column", () => {
    const onUpdateIssue = vi.fn();
    const issues = [i("a", "todo")];
    __test_handleDragEnd(
      issues,
      onUpdateIssue,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { active: { id: "a" }, over: { id: "todo" } } as any,
    );
    expect(onUpdateIssue).not.toHaveBeenCalled();
  });

  it("__test_handleDragEnd resolves card-id drop to the card's column status", () => {
    const onUpdateIssue = vi.fn();
    const issues = [i("a", "todo"), i("b", "done")];
    __test_handleDragEnd(
      issues,
      onUpdateIssue,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { active: { id: "a" }, over: { id: "b" } } as any,
    );
    expect(onUpdateIssue).toHaveBeenCalledWith("a", { status: "done" });
  });
});
