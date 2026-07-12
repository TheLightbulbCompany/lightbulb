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
  if (path === "/issues")
    return {
      data: { issues: seededIssues },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    };
  return { data: { agents: [], projects: [] }, isLoading: false, error: null, mutate: vi.fn() };
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

  it("re-opens the dialog when newTask goes false→true on a mounted panel", () => {
    const { rerender } = render(<IssuesPanel newTask={false} />);
    expect(screen.queryByTestId("new-issue-dialog-stub")).not.toBeInTheDocument();
    rerender(<IssuesPanel newTask />);
    expect(screen.getByTestId("new-issue-dialog-stub")).toBeInTheDocument();
  });
});
