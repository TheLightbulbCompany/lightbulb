import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TasksList } from "@/components/teams/issues/TasksList";
import type { Issue } from "@/components/teams/shared/types";

function issue(partial: Partial<Issue> & Pick<Issue, "id" | "title" | "status">): Issue {
  return { identifier: partial.id.toUpperCase(), priority: null, ...partial };
}

const onUpdateIssue = vi.fn();
const onNewTask = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
});

describe("TasksList", () => {
  it("renders the empty state with a New task action when there are no issues", () => {
    render(
      <TasksList issues={[]} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    expect(screen.getByText(/no tasks yet/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    expect(onNewTask).toHaveBeenCalledTimes(1);
  });

  it("nests sub-tasks under their parent and collapses on chevron toggle", () => {
    const issues = [
      issue({ id: "iso-1", title: "Parent task", status: "todo" }),
      issue({ id: "iso-2", title: "Child task", status: "todo", parentId: "iso-1" }),
    ];
    render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    // Both rows render expanded by default.
    expect(screen.getByText("Parent task")).toBeInTheDocument();
    expect(screen.getByText("Child task")).toBeInTheDocument();

    // Collapsing the parent hides the child + shows the sub-task count.
    fireEvent.click(screen.getByRole("button", { name: /collapse sub-tasks/i }));
    expect(screen.queryByText("Child task")).not.toBeInTheDocument();
    expect(screen.getByText(/1 sub-task/)).toBeInTheDocument();
  });

  it("clicking a status option in the circle menu patches the issue status", async () => {
    const user = userEvent.setup();
    const issues = [issue({ id: "iso-9", title: "Ship it", status: "todo" })];
    render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    // Open the status menu via the circle (accessible name = status label).
    // userEvent fires the pointer sequence Radix's Popover trigger needs.
    await user.click(screen.getByLabelText(/^todo$/i));
    // Pick "Done" from the portalled popover and assert the patch fires. Each
    // menu item is a <Button> whose visible text is the status label; match on
    // text (the button also embeds a nested StatusIcon, so role+name is noisy).
    const done = await screen.findByText("Done");
    await user.click(done);
    expect(onUpdateIssue).toHaveBeenCalledWith("iso-9", { status: "done" });
  });

  it("switches to the board view when the board toggle is pressed", () => {
    const issues = [issue({ id: "iso-3", title: "Board task", status: "in_progress" })];
    const { container } = render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    // No kanban columns in list mode.
    expect(container.querySelector("[data-kanban-column]")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /board view/i }));
    // Board renders the 7 status columns, with the task in its column.
    const inProg = container.querySelector('[data-kanban-column="in_progress"]');
    expect(inProg).not.toBeNull();
    expect(within(inProg as HTMLElement).getByText("Board task")).toBeInTheDocument();
  });
});
