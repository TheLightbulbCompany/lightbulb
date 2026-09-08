// Test for IssuesPanel (the Tasks surface).
//
// After the Tasks rebuild, the panel renders `TasksList` (list/board tree)
// instead of the old 3-column PanelDataTable. This file guards the
// integration: the masthead reads "Tasks", the empty state copy is "No tasks
// yet", and clicking "New task" mounts NewIssueDialog with open=true. The
// actual POST /issues body shape is asserted in the NewIssueDialog and
// useIssueMutations tests.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import type { Issue } from "@/components/teams/shared/types";

// Path-aware read mock shared across all tests. `seededIssues` is reset per
// test so the empty-state cases stay green while the actions-column case can
// seed a row. `/agents` + `/projects` always resolve to empty arrays (the
// panel reads them only to populate NewIssueDialog's pickers).
let seededIssues: Issue[] = [];
const mockMutate = vi.fn();
const mockRead = vi.fn((path: string) => {
  // Prefix-match: the panel's issues key carries the routine-filter query
  // string (`/issues?excludeRoutineExecutions=true`) unless the toggle is on.
  if (path.startsWith("/issues"))
    return {
      data: { issues: seededIssues },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    };
  return {
    data: { agents: [], projects: [], goals: [] },
    isLoading: false,
    error: null,
    mutate: vi.fn(),
  };
});

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

// IssueStatusMenu (rendered per row in the actions column) pulls
// useIssueMutations; stub it so the panel renders standalone.
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ update: vi.fn(), create: vi.fn(), createChild: vi.fn(), addComment: vi.fn() }),
}));

const newIssueDialogStub = vi.fn();
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: (props: {
    open: boolean;
    onOpenChange: (next: boolean) => void;
    onCreated?: (id: string) => void;
  }) => {
    newIssueDialogStub(props);
    return props.open ? <div data-testid="new-issue-dialog-stub" /> : null;
  },
}));

import { IssuesPanel } from "@/components/teams/panels/IssuesPanel";

beforeEach(() => {
  seededIssues = [];
});

describe("IssuesPanel", () => {
  it("renders the editorial empty state when there are no tasks", () => {
    render(<IssuesPanel />);
    expect(
      screen.getByRole("heading", { level: 1, name: /tasks/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/no tasks yet/i)).toBeInTheDocument();
  });

  it("clicking 'New task' opens NewIssueDialog (open=true)", () => {
    render(<IssuesPanel />);
    expect(screen.queryByTestId("new-issue-dialog-stub")).not.toBeInTheDocument();
    // Two New task CTAs render when the list is empty (masthead + empty
    // state). Click the first.
    const buttons = screen.getAllByRole("button", { name: /new task/i });
    fireEvent.click(buttons[0]);
    expect(screen.getByTestId("new-issue-dialog-stub")).toBeInTheDocument();
  });

  it("renders a task row with a clickable status circle", () => {
    seededIssues = [
      { id: "i1", title: "Fix the thing", status: "todo" } as Issue,
    ];
    render(<IssuesPanel />);
    expect(screen.getByText("Fix the thing")).toBeInTheDocument();
    // The status circle is the inline easy-action — it carries the status
    // label as its accessible name so it can be opened to change status.
    expect(screen.getByLabelText(/todo/i)).toBeInTheDocument();
  });

  // Prop-driven create-dialog intent (was the ?new=task URL read). The mobile
  // shell now hands `newTask` down as a prop and strips its own URL param when
  // the panel reports consumption via onNewTaskConsumed.
  it("opens the create dialog from the newTask prop and reports consumption on close", () => {
    const onNewTaskConsumed = vi.fn();
    render(<IssuesPanel newTask onNewTaskConsumed={onNewTaskConsumed} />);
    expect(screen.getByTestId("new-issue-dialog-stub")).toBeInTheDocument();
    // Drive the dialog's close the way NewIssueDialog would (Cancel / Esc):
    // the panel's onOpenChange(false) path must fire onNewTaskConsumed.
    const lastProps = newIssueDialogStub.mock.calls.at(-1)![0] as {
      onOpenChange: (next: boolean) => void;
    };
    act(() => lastProps.onOpenChange(false));
    expect(onNewTaskConsumed).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("new-issue-dialog-stub")).not.toBeInTheDocument();
  });

  // Get Started's "Create your first task" step moved here with Inbox's issue
  // rows; it opens the SAME dialog, pre-filled with a CEO-directed task.
  it("pre-fills the composer for the onboarding first task, and only then", () => {
    render(<IssuesPanel newTask newTaskPrefill="onboarding" />);
    const props = newIssueDialogStub.mock.calls.at(-1)![0] as {
      defaultTitle?: string;
      defaultStatus?: string;
    };
    expect(props.defaultTitle).toContain("[your goal]");
    expect(props.defaultStatus).toBe("todo");
  });

  it("leaves the composer blank for a plain New task intent", () => {
    render(<IssuesPanel newTask />);
    const props = newIssueDialogStub.mock.calls.at(-1)![0] as {
      defaultTitle?: string;
      defaultStatus?: string;
    };
    expect(props.defaultTitle).toBeUndefined();
    expect(props.defaultStatus).toBeUndefined();
  });

  it("re-opens the dialog when newTask goes false→true on a mounted panel", () => {
    const { rerender } = render(<IssuesPanel newTask={false} />);
    expect(screen.queryByTestId("new-issue-dialog-stub")).not.toBeInTheDocument();
    rerender(<IssuesPanel newTask />);
    expect(screen.getByTestId("new-issue-dialog-stub")).toBeInTheDocument();
  });

  // Regression guard for the live-update bug: the board once stayed "No tasks
  // yet" for minutes while GET /issues already returned the rows, because the
  // event-driven invalidation (TeamsEventsProvider → activity.logged) rides a
  // replay-less bridge that can miss agent-created issues, and nothing else
  // revalidated this key (revalidateOnFocus is off globally; dock panels are
  // hidden, not unmounted, on switch). The `/issues` read must carry a polling
  // + focus-revalidation backstop so it self-heals.
  it("reads /issues with a 30s poll + focus-revalidation backstop", () => {
    mockRead.mockClear();
    render(<IssuesPanel />);
    expect(mockRead).toHaveBeenCalledWith(
      expect.stringMatching(/^\/issues(\?|$)/),
      expect.objectContaining({
        refreshInterval: 30_000,
        revalidateOnFocus: true,
      }),
    );
  });

  // Routine-spawned tasks (one row per firing, forever) drown out hand-filed
  // work, so the panel asks upstream to drop them by default. Filtering MUST
  // stay server-side: the list is capped by `limit`, so a client-side drop
  // would just return fewer real tasks per page.
  it("excludes routine tasks by default and includes them when toggled on", () => {
    mockRead.mockClear();
    render(<IssuesPanel />);
    expect(mockRead).toHaveBeenCalledWith(
      "/issues?excludeRoutineExecutions=true",
      expect.anything(),
    );

    const toggle = screen.getByRole("switch", { name: /routine tasks/i });
    expect(toggle).toHaveAttribute("aria-checked", "false");

    mockRead.mockClear();
    fireEvent.click(toggle);
    expect(mockRead).toHaveBeenCalledWith("/issues", expect.anything());
    expect(
      screen.getByRole("switch", { name: /routine tasks/i }),
    ).toHaveAttribute("aria-checked", "true");
  });

  // The Group control is an icon button (aria-label "Group") opening a
  // popover of options — not a <select>.
  // Grouping is a list-view affordance and board is the default view, so a
  // test that wants the Group menu has to switch first.
  const goToListView = () => {
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "List view" }));
    });
  };
  const pickGrouping = (label: string) => {
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Group" }));
    });
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: label }));
    });
  };

  it("offers New goal for the grouping the owner is LOOKING AT, not the one the URL opened on", () => {
    // The bug this pins: gating on the deep-link prop meant someone who
    // grouped by goal from the menu saw no "New goal" — and a company with no
    // goals yet has no goal link to arrive by, so that was the only path to
    // its first goal.
    render(<IssuesPanel />);
    goToListView();
    expect(screen.queryByRole("button", { name: "New goal" })).toBeNull();
    pickGrouping("Goal");
    expect(screen.getByRole("button", { name: "New goal" })).toBeInTheDocument();
  });

  it("offers New project once the owner groups by project", () => {
    render(<IssuesPanel />);
    goToListView();
    expect(screen.queryByRole("button", { name: "New project" })).toBeNull();
    pickGrouping("Project");
    expect(screen.getByRole("button", { name: "New project" })).toBeInTheDocument();
  });

  it("drops New goal when the owner regroups away from a goal deep link", () => {
    render(<IssuesPanel initialGroupBy="goal" />);
    expect(screen.getByRole("button", { name: "New goal" })).toBeInTheDocument();
    pickGrouping("Status");
    expect(screen.queryByRole("button", { name: "New goal" })).toBeNull();
  });
});
