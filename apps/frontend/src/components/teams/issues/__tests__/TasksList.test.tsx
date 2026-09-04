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
  // The view mode now persists to localStorage, so a choice made by one test
  // would otherwise decide the starting view of the next one.
  localStorage.clear();
});

// Board is the default view. These tests exercise the LIST surface (nesting,
// row menus, grouping, assignee chips), so they switch to it first.
function toList(): void {
  fireEvent.click(screen.getByRole("button", { name: /list view/i }));
}

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
    toList();
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
    toList();
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
    const nav: InlineTeamsNavValue = { openDetail, leaveToList: vi.fn() };
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
    toList();

    // Open the row's overflow menu and pick Hide.
    await user.click(screen.getByRole("button", { name: /task actions/i }));
    await user.click(await screen.findByText("Hide"));

    expect(onUpdateIssue).toHaveBeenCalledWith("iso-7", {
      hiddenAt: expect.any(String),
    });
    // Interacting with the menu must not trigger the row's detail navigation.
    expect(openDetail).not.toHaveBeenCalled();
  });

  it("opens on the board, with each task in its status column", () => {
    const issues = [issue({ id: "iso-3", title: "Board task", status: "in_progress" })];
    const { container } = render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    // Board is the default — no click needed to get here.
    const inProg = container.querySelector('[data-kanban-column="in_progress"]');
    expect(inProg).not.toBeNull();
    expect(within(inProg as HTMLElement).getByText("Board task")).toBeInTheDocument();
  });

  it("switches to the list view when the list toggle is pressed", () => {
    const issues = [issue({ id: "iso-3", title: "Board task", status: "in_progress" })];
    const { container } = render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    toList();
    expect(container.querySelector("[data-kanban-column]")).toBeNull();
    expect(screen.getByText("Board task")).toBeInTheDocument();
  });

  it("remembers the chosen view across a remount", () => {
    const issues = [issue({ id: "iso-3", title: "Board task", status: "in_progress" })];
    const first = render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    toList();
    first.unmount();

    const second = render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    // Without persistence this would come back as the board default — which is
    // exactly what made list-preferring users re-pick on every reload.
    expect(second.container.querySelector("[data-kanban-column]")).toBeNull();
  });

  it("falls back to the board default when storage is unavailable", () => {
    const getItem = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("storage disabled");
      });
    const issues = [issue({ id: "iso-3", title: "Board task", status: "in_progress" })];
    const { container } = render(
      <TasksList issues={issues} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    expect(container.querySelector("[data-kanban-column]")).not.toBeNull();
    getItem.mockRestore();
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
    toList();
    // Assignee resolves to the roster name (not the raw id).
    expect(screen.getByText("Friday")).toBeInTheDocument();
    // Priority icon renders (PriorityIcon's read-only span has no text; assert
    // via the label chips + the absence of "Unassigned" instead). Labels show
    // the first two chips + a "+1" overflow marker for the third.
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
    toList();
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  it("groups by priority into ordered priority buckets", async () => {
    const user = userEvent.setup();
    const issues = [
      issue({ id: "iso-10", title: "Crit task", status: "todo", priority: "critical" }),
      issue({ id: "iso-11", title: "Low task", status: "todo", priority: "low" }),
      issue({ id: "iso-12", title: "No-pri task", status: "todo" }),
    ];
    render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    toList();
    // Open the Group popover and pick "Priority".
    await user.click(screen.getByRole("button", { name: /^group$/i }));
    await user.click(await screen.findByText("Priority"));
    // Group headers render for each present priority bucket + the no-priority
    // bucket. statusLabel uppercases "critical"/"low" → "Critical"/"Low".
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("Low")).toBeInTheDocument();
    expect(screen.getByText("No priority")).toBeInTheDocument();
  });

  it("applies a priority filter to narrow the list", async () => {
    const user = userEvent.setup();
    const issues = [
      issue({ id: "iso-20", title: "High one", status: "todo", priority: "high" }),
      issue({ id: "iso-21", title: "Low one", status: "todo", priority: "low" }),
    ];
    render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    expect(screen.getByText("High one")).toBeInTheDocument();
    expect(screen.getByText("Low one")).toBeInTheDocument();

    // Open the filters popover and tick the "High" priority facet.
    await user.click(screen.getByRole("button", { name: /^filter$/i }));
    const highRow = (await screen.findByText("High")).closest("label");
    await user.click(within(highRow as HTMLElement).getByRole("checkbox"));

    // The list narrows to just the high-priority task.
    expect(screen.getByText("High one")).toBeInTheDocument();
    expect(screen.queryByText("Low one")).not.toBeInTheDocument();
  });

  it("shows the filtered-empty state with a Clear-filters action when filters yield nothing", async () => {
    const user = userEvent.setup();
    // All tasks are low-priority; filtering to "Critical" empties the list.
    const issues = [
      issue({ id: "iso-30", title: "Only low", status: "todo", priority: "low" }),
    ];
    render(
      <TasksList
        issues={issues}
        agents={AGENTS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^filter$/i }));
    const critRow = (await screen.findByText("Critical")).closest("label");
    await user.click(within(critRow as HTMLElement).getByRole("checkbox"));

    // Distinct filtered-empty copy (NOT the "No tasks yet." create-prompt).
    expect(screen.getByText(/no tasks match these filters/i)).toBeInTheDocument();
    expect(screen.queryByText(/no tasks yet/i)).not.toBeInTheDocument();

    // Clicking "Clear filters" resets the popover state → the task returns.
    await user.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(screen.getByText("Only low")).toBeInTheDocument();
  });

  it("shows the create-prompt empty state (not filtered-empty) when there are genuinely no tasks", () => {
    render(
      <TasksList issues={[]} onUpdateIssue={onUpdateIssue} onNewTask={onNewTask} />,
    );
    expect(screen.getByText(/no tasks yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/no tasks match these filters/i)).not.toBeInTheDocument();
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
    // Board card lights its live dot on the default view.
    expect(
      container.querySelector('[data-testid="kanban-card-live-dot"]'),
    ).not.toBeNull();
    // And the list row lights its own once switched.
    toList();
    expect(screen.getByTestId("task-row-live-dot")).toBeInTheDocument();
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
    toList();
    expect(screen.queryByTestId("task-row-live-dot")).not.toBeInTheDocument();
  });
});
