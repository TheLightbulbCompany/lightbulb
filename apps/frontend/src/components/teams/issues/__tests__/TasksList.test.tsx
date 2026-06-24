import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TasksList } from "@/components/teams/issues/TasksList";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";
import type { Issue } from "@/components/teams/shared/types";

function issue(partial: Partial<Issue> & Pick<Issue, "id" | "title" | "status">): Issue {
  return { identifier: partial.id.toUpperCase(), priority: null, ...partial };
}

// Roster passed to TasksList so it can resolve assignee agent ids to names.
const AGENTS: { id: string; name: string }[] = [
  { id: "agent-friday", name: "Friday" },
  { id: "agent-atlas", name: "Atlas" },
];

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

  it("hides a task from the row menu without navigating the row link", async () => {
    const user = userEvent.setup();
    const openDetail = vi.fn();
    const nav: InlineTeamsNavValue = { openDetail };
    const issues = [issue({ id: "iso-7", title: "Archive me", status: "todo" })];
    render(
      <InlineTeamsNavProvider value={nav}>
        <TasksList
          issues={issues}
          onUpdateIssue={onUpdateIssue}
          onNewTask={onNewTask}
        />
      </InlineTeamsNavProvider>,
    );

    // Open the row's overflow menu and pick Hide.
    await user.click(screen.getByRole("button", { name: /task actions/i }));
    await user.click(await screen.findByText("Hide"));

    expect(onUpdateIssue).toHaveBeenCalledWith("iso-7", {
      hiddenAt: expect.any(String),
    });
    // Interacting with the menu must not trigger the row's detail navigation.
    expect(openDetail).not.toHaveBeenCalled();
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

  it("renders an assignee chip, priority icon, and label chips on a row", () => {
    const issues = [
      issue({
        id: "iso-5",
        title: "Rich row",
        status: "todo",
        priority: "high",
        assigneeAgentId: "agent-friday",
        labels: [
          { id: "l1", name: "backend" },
          { id: "l2", name: "urgent" },
          { id: "l3", name: "overflow" },
        ],
      }),
    ];
    render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    // Assignee resolves to the roster name (not the raw id).
    expect(screen.getByText("Friday")).toBeInTheDocument();
    // Labels show the first two chips + a "+1" overflow marker for the third
    // (the priority icon is glyph-only, so the chips carry the assertion).
    expect(screen.getByText("backend")).toBeInTheDocument();
    expect(screen.getByText("urgent")).toBeInTheDocument();
    expect(screen.getByText("+1")).toBeInTheDocument();
    expect(screen.queryByText("overflow")).not.toBeInTheDocument();
    expect(screen.queryByText("Unassigned")).not.toBeInTheDocument();
  });

  it("renders Unassigned when a row has no assignee", () => {
    const issues = [issue({ id: "iso-6", title: "Orphan", status: "todo" })];
    render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  it("lights the board live indicator for tasks in liveIssueIds", () => {
    const issues = [
      issue({ id: "iso-40", title: "Running task", status: "in_progress" }),
    ];
    const { container } = render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        liveIssueIds={new Set(["iso-40"])}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    // List row lights its live dot.
    expect(screen.getByTestId("task-row-live-dot")).toBeInTheDocument();
    // And the board card lights its live dot once switched to board view.
    fireEvent.click(screen.getByRole("button", { name: /board view/i }));
    expect(
      container.querySelector('[data-testid="kanban-card-live-dot"]'),
    ).not.toBeNull();
  });

  it("does not light the live indicator for tasks absent from liveIssueIds", () => {
    const issues = [issue({ id: "iso-41", title: "Idle task", status: "todo" })];
    render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        liveIssueIds={new Set(["some-other-id"])}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    expect(screen.queryByTestId("task-row-live-dot")).not.toBeInTheDocument();
  });
});
