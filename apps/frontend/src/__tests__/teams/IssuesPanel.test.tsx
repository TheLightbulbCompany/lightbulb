// Test for IssuesPanel.
//
// After PR 4 of the /teams panel port, the inline `CreateIssueDialog` was
// deleted in favour of the standalone `NewIssueDialog` (which uses
// `useIssueMutations.create()` and has its own behaviour tests at
// src/__tests__/components/teams/issues/NewIssueDialog.test.tsx). This file
// guards the integration: clicking "New issue" mounts NewIssueDialog with
// open=true. The actual POST /issues body shape is asserted in the
// NewIssueDialog and useIssueMutations tests.
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const mockMutate = vi.fn();
const mockRead = vi.fn(() => ({
  data: { issues: [] },
  isLoading: false,
  error: null,
  mutate: mockMutate,
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
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

describe("IssuesPanel", () => {
  it("renders the editorial empty state when there are no issues", () => {
    render(<IssuesPanel />);
    expect(
      screen.getByRole("heading", { level: 1, name: /issues/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/no issues yet/i)).toBeInTheDocument();
  });

  it("clicking 'New issue' opens NewIssueDialog (open=true)", () => {
    render(<IssuesPanel />);
    expect(screen.queryByTestId("new-issue-dialog-stub")).not.toBeInTheDocument();
    // Two New issue CTAs render when the list is empty (masthead + empty
    // state). Click the first.
    const buttons = screen.getAllByRole("button", { name: /new issue/i });
    fireEvent.click(buttons[0]);
    expect(screen.getByTestId("new-issue-dialog-stub")).toBeInTheDocument();
  });
});
