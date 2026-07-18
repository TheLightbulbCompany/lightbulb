// apps/frontend/src/__tests__/teams/IssueTreeControlSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { ReactNode } from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";

const read = vi.fn();
const post = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read, post }) }));

// Deterministic stand-ins for the shell primitives so the dialog's footer +
// body render inline (Radix portals are awkward to assert against).
vi.mock("@/components/teams/shell", () => ({
  EditorialButton: ({
    children,
    onClick,
    disabled,
  }: {
    children?: ReactNode;
    onClick?: () => void;
    disabled?: boolean;
  }) => (
    <button onClick={onClick} disabled={disabled}>
      {children}
    </button>
  ),
  EditorialDialog: ({
    open,
    title,
    children,
    footer,
  }: {
    open: boolean;
    title?: ReactNode;
    children?: ReactNode;
    footer?: ReactNode;
  }) =>
    open ? (
      <div role="dialog">
        <div>{title}</div>
        {children}
        {footer}
      </div>
    ) : null,
}));

vi.mock("@/components/ui/textarea", () => ({
  Textarea: (props: Record<string, unknown>) => <textarea {...props} />,
}));

import { IssueTreeControlSection } from "@/components/teams/issues/IssueTreeControlSection";

beforeEach(() => {
  read.mockReset();
  post.mockReset();
  post.mockResolvedValue({});
});

describe("IssueTreeControlSection", () => {
  it("renders nothing when the state read errors (board-only)", () => {
    read.mockReturnValue({ data: undefined, error: new Error("403"), mutate: vi.fn() });
    const { container } = render(<IssueTreeControlSection issueId="iss1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("offers Pause + Cancel when the subtree is not paused", () => {
    read.mockReturnValue({ data: { activePauseHold: null }, error: null, mutate: vi.fn() });
    render(<IssueTreeControlSection issueId="iss1" />);
    expect(screen.getByRole("button", { name: "Pause subtree" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel subtree" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume subtree" })).not.toBeInTheDocument();
  });

  it("offers Resume when paused at the root", () => {
    read.mockReturnValue({
      data: { activePauseHold: { holdId: "h1", isRoot: true, issueId: "iss1", rootIssueId: "iss1", reason: null } },
      error: null,
      mutate: vi.fn(),
    });
    render(<IssueTreeControlSection issueId="iss1" />);
    expect(screen.getByRole("button", { name: "Resume subtree" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause subtree" })).not.toBeInTheDocument();
  });

  it("shows an ancestor-hold notice (no resume button) when paused below the root", () => {
    read.mockReturnValue({
      data: { activePauseHold: { holdId: "h1", isRoot: false, issueId: "iss1", rootIssueId: "root", reason: null } },
      error: null,
      mutate: vi.fn(),
    });
    render(<IssueTreeControlSection issueId="iss1" />);
    expect(screen.getByText(/ancestor subtree hold/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume subtree" })).not.toBeInTheDocument();
  });

  it("posts a create-hold with mode=pause on confirm, then revalidates state", async () => {
    const mutate = vi.fn().mockResolvedValue(undefined);
    read.mockReturnValue({ data: { activePauseHold: null }, error: null, mutate });
    render(<IssueTreeControlSection issueId="iss1" />);

    fireEvent.click(screen.getByRole("button", { name: "Pause subtree" }));

    // Dialog is open — confirm from within it (the section button shares its label).
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Pause subtree" }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/issues/iss1/tree-holds", { mode: "pause" }),
    );
    await waitFor(() => expect(mutate).toHaveBeenCalled());
    // A best-effort preview is also fetched when the dialog opens.
    expect(post).toHaveBeenCalledWith("/issues/iss1/tree-control/preview", { mode: "pause" });
  });
});
