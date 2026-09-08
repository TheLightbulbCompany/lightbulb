// Goals and Projects stopped being tabs and became groupings of THIS page, so
// what used to be "does the Goals tab render" is now "does the goal grouping
// resolve titles, count progress, and stay reachable".

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GROUP_OPTIONS, TasksList } from "@/components/teams/issues/TasksList";
import { TASKS_GROUP_BY } from "@/components/workspace/panelRegistry";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";
import type { Goal, Issue } from "@/components/teams/shared/types";

function issue(partial: Partial<Issue> & Pick<Issue, "id" | "title" | "status">): Issue {
  return { identifier: partial.id.toUpperCase(), priority: null, ...partial };
}

const GOALS: Goal[] = [
  { id: "goal_ship", title: "Ship the launch", level: "company", status: "active" },
  { id: "goal_hire", title: "Hire two engineers", level: "company", status: "active" },
];

const onUpdateIssue = vi.fn();
const onNewTask = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe("Tasks — the deep-link fence", () => {
  it("offers exactly the groupings a ?groupBy link may name", () => {
    expect(new Set(GROUP_OPTIONS.map(([value]) => value))).toEqual(TASKS_GROUP_BY);
  });

  it("opens on the requested grouping", () => {
    render(
      <TasksList
        issues={[issue({ id: "iso-1", title: "A", status: "todo", goalId: "goal_ship" })]}
        goals={GOALS}
        initialGroupBy="goal"
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    expect(screen.getByText("Ship the launch")).toBeInTheDocument();
  });

  it("falls back to no grouping when the router hands it nothing", () => {
    render(
      <TasksList
        issues={[issue({ id: "iso-1", title: "A", status: "todo", goalId: "goal_ship" })]}
        goals={GOALS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    // Board is the default view when no grouping was asked for, so there is no
    // group heading at all — and certainly not a goal one.
    expect(screen.queryByText("Ship the launch")).toBeNull();
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("drops the grouping when a plain Tasks nav clears the param", () => {
    const props = {
      issues: [issue({ id: "iso-1", title: "A", status: "todo", goalId: "goal_ship" })],
      goals: GOALS,
      onUpdateIssue,
      onNewTask,
    };
    const { rerender } = render(<TasksList {...props} initialGroupBy="goal" />);
    expect(screen.getByText("Ship the launch")).toBeInTheDocument();
    rerender(<TasksList {...props} />);
    expect(screen.queryByText("Ship the launch")).toBeNull();
  });
});

describe("Tasks — grouped by goal", () => {
  const issues = [
    issue({ id: "iso-1", title: "Write the copy", status: "done", goalId: "goal_ship" }),
    issue({ id: "iso-2", title: "Cut the build", status: "todo", goalId: "goal_ship" }),
    issue({ id: "iso-3", title: "Post the ad", status: "todo", goalId: "goal_hire" }),
    issue({ id: "iso-4", title: "Unrelated", status: "todo" }),
  ];

  function renderGrouped(nav?: InlineTeamsNavValue) {
    const list = (
      <TasksList
        issues={issues}
        goals={GOALS}
        initialGroupBy="goal"
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />
    );
    return render(
      nav ? <InlineTeamsNavProvider value={nav}>{list}</InlineTeamsNavProvider> : list,
    );
  }

  it("heads each group with the goal's TITLE, never its raw id", () => {
    renderGrouped();
    expect(screen.getByText("Ship the launch")).toBeInTheDocument();
    expect(screen.getByText("Hire two engineers")).toBeInTheDocument();
    expect(screen.queryByText(/goal_/)).toBeNull();
  });

  it("says so honestly when a goal is not in the roster", () => {
    render(
      <TasksList
        issues={[issue({ id: "iso-9", title: "Orphan", status: "todo", goalId: "goal_gone" })]}
        goals={GOALS}
        initialGroupBy="goal"
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    expect(screen.getByText("Unknown goal")).toBeInTheDocument();
    expect(screen.queryByText(/goal_gone/)).toBeNull();
  });

  it("buckets tasks with no goal under a named group, sorted last", () => {
    renderGrouped();
    const headings = screen
      .getAllByText(/Ship the launch|Hire two engineers|No goal/)
      .map((el) => el.textContent);
    expect(headings.at(-1)).toBe("No goal");
  });

  it("counts N of M done per goal", () => {
    renderGrouped();
    // Ship the launch has two tasks, one done; Hire two engineers and the
    // no-goal bucket have one each, neither done.
    expect(screen.getByText("1 of 2 done")).toBeInTheDocument();
    expect(screen.getAllByText("0 of 1 done")).toHaveLength(2);
  });

  it("does not count cancelled work as outstanding", () => {
    render(
      <TasksList
        issues={[
          issue({ id: "iso-1", title: "A", status: "done", goalId: "goal_ship" }),
          issue({ id: "iso-2", title: "B", status: "cancelled", goalId: "goal_ship" }),
        ]}
        goals={GOALS}
        initialGroupBy="goal"
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    expect(screen.getByText("1 of 1 done")).toBeInTheDocument();
  });

  it("links the heading to the goal's detail so its description stays reachable", () => {
    renderGrouped();
    const link = screen.getByRole("link", { name: "Ship the launch" });
    expect(link).toHaveAttribute(
      "href",
      "/chat?panel=teams-goal-detail&detailId=goal_ship",
    );
  });

  it("opens the goal in-shell on a plain click when a nav provider is present", () => {
    const nav: InlineTeamsNavValue = { openDetail: vi.fn(), leaveToList: vi.fn() };
    renderGrouped(nav);
    fireEvent.click(screen.getByRole("link", { name: "Ship the launch" }), { button: 0 });
    expect(nav.openDetail).toHaveBeenCalledWith("goal-detail", "goal_ship");
  });

  it("gives the ungrouped bucket nothing to open", () => {
    renderGrouped();
    expect(screen.queryByRole("link", { name: "No goal" })).toBeNull();
  });

  it("is pickable by hand from the Group menu", async () => {
    const user = userEvent.setup();
    render(
      <TasksList
        issues={issues}
        goals={GOALS}
        onUpdateIssue={onUpdateIssue}
        onNewTask={onNewTask}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /list view/i }));
    await user.click(screen.getByRole("button", { name: /^group$/i }));
    await user.click(await screen.findByText("Goal"));
    expect(screen.getByText("Ship the launch")).toBeInTheDocument();
  });

  it("hands the view back to the board when the owner asks for it", () => {
    renderGrouped();
    expect(screen.getByText("Ship the launch")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /board view/i }));
    expect(screen.queryByText("Ship the launch")).toBeNull();
  });
});
