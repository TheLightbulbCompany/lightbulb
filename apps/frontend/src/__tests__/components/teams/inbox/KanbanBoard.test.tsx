import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { KanbanBoard, BOARD_STATUSES, boardColumns, __test_handleDragEnd } from "@/components/teams/inbox/KanbanBoard";
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

  // This test previously asserted the opposite — that an issue whose status is
  // outside BOARD_STATUSES does NOT render — which codified silent data loss as
  // intended behaviour. It was survivable while the board was opt-in; it is not
  // survivable now the board is the default view for Tasks. `pending`, `review`,
  // `won_t_do` and `duplicate` are all in ISSUE_STATUSES (what
  // `searchQueryParser` calls the board-column set) and none of them are in
  // BOARD_STATUSES, so real, user-settable tasks were vanishing.
  it("renders an issue whose status is outside BOARD_STATUSES, in its own column", () => {
    const issues = [i("a", "todo"), i("b", "pending" as Issue["status"])];
    const { container } = render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);
    expect(container.querySelector('[data-kanban-card-id="a"]')).toBeInTheDocument();
    expect(container.querySelector('[data-kanban-card-id="b"]')).toBeInTheDocument();
    expect(container.querySelector('[data-kanban-column="pending"]')).toBeInTheDocument();
  });

  it("no issue is ever dropped, whatever its status", () => {
    const odd = ["pending", "review", "won_t_do", "duplicate", "open", "closed", "totally_new"];
    const issues = odd.map((s, n) => i(`x${n}`, s as Issue["status"]));
    const { container } = render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);
    expect(container.querySelectorAll("[data-kanban-card-id]")).toHaveLength(odd.length);
  });

  it("derived columns come after the canonical seven, in a stable order", () => {
    const issues = [i("a", "review" as Issue["status"]), i("b", "pending" as Issue["status"])];
    const { container } = render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);
    const rendered = [...container.querySelectorAll("[data-kanban-column]")].map((el) =>
      el.getAttribute("data-kanban-column"),
    );
    expect(rendered).toEqual([...BOARD_STATUSES, "pending", "review"]);
  });

  it("boardColumns renders the canonical seven even with no issues at all", () => {
    expect(boardColumns([])).toEqual([...BOARD_STATUSES]);
  });

  it("a derived column is a real drop target, not a one-way door", () => {
    const issues = [i("a", "todo")];
    const onUpdate = vi.fn();
    __test_handleDragEnd(issues, onUpdate, { active: { id: "a" }, over: { id: "pending" } } as never, [
      ...BOARD_STATUSES,
      "pending",
    ]);
    expect(onUpdate).toHaveBeenCalledWith("a", { status: "pending" });
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

  it("has no per-card status dropdowns", () => {
    render(<KanbanBoard issues={[i("a", "todo"), i("b", "done")]} onUpdateIssue={vi.fn()} />);
    expect(screen.queryAllByRole("combobox")).toHaveLength(0);
  });
});
