// apps/frontend/tests/unit/components/teams/panels/IssuesPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelToolbar +
// PanelEmptyState + PanelDataTable composition on the Issues panel + delegates
// the new-issue create flow to the standalone NewIssueDialog component.
// Behavior tests (whitelisted POST body, etc.) live in
// src/__tests__/teams/IssuesPanel.test.tsx.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockPost = vi.fn();
const mockMutate = vi.fn();
const mockRead = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

// Stub NewIssueDialog so we can assert that the panel routes the New-issue CTA
// through it (instead of an inline CreateIssueDialog) without depending on the
// real dialog's internals.
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="new-issue-dialog-stub" role="dialog" /> : null,
}));

import { IssuesPanel } from "@/components/teams/panels/IssuesPanel";

describe("IssuesPanel atom contract", () => {
  beforeEach(() => {
    mockPost.mockReset();
    mockMutate.mockReset();
    mockRead.mockReset();
  });

  it("renders the editorial PanelMasthead with overline + h1 title", () => {
    mockRead.mockReturnValue({
      data: { issues: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<IssuesPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /issues/i }),
    ).toBeInTheDocument();
  });

  it("renders the editorial PanelToolbar search input (role=searchbox)", () => {
    mockRead.mockReturnValue({
      data: { issues: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<IssuesPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState shape (h2 with editorial Lora-italic class) when issues list is empty", () => {
    mockRead.mockReturnValue({
      data: { issues: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<IssuesPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no issues yet/i);
  });

  it("renders New issue CTA via EditorialButton in the masthead actions", () => {
    mockRead.mockReturnValue({
      data: { issues: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<IssuesPanel />);
    const newIssueButtons = screen.getAllByRole("button", {
      name: /new issue/i,
    });
    expect(newIssueButtons.length).toBeGreaterThan(0);
  });

  it("opens the standalone NewIssueDialog (not an inline create dialog) when New issue is clicked", async () => {
    mockRead.mockReturnValue({
      data: { issues: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    const user = userEvent.setup();
    render(<IssuesPanel />);

    expect(screen.queryByTestId("new-issue-dialog-stub")).not.toBeInTheDocument();
    const ctas = screen.getAllByRole("button", { name: /new issue/i });
    await user.click(ctas[0]);
    expect(screen.getByTestId("new-issue-dialog-stub")).toBeInTheDocument();
  });

  it("filters rows by search query (case-insensitive substring match on title)", async () => {
    mockRead.mockReturnValue({
      data: {
        issues: [
          { id: "i1", title: "Fix login bug", status: "open" },
          { id: "i2", title: "Improve dashboard", status: "open" },
        ],
      },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    const user = userEvent.setup();
    render(<IssuesPanel />);

    expect(screen.getByText("Fix login bug")).toBeInTheDocument();
    expect(screen.getByText("Improve dashboard")).toBeInTheDocument();

    const searchbox = screen.getByRole("searchbox");
    await user.type(searchbox, "dashboard");

    expect(screen.queryByText("Fix login bug")).not.toBeInTheDocument();
    expect(screen.getByText("Improve dashboard")).toBeInTheDocument();
  });
});
