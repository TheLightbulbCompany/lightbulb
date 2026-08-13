import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { WorkspaceDemo } from "../WorkspaceDemo";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

// MessageList is next/dynamic-loaded (it drags in Prism + react-markdown);
// the demo test targets the Teams surfaces, so stub the dynamic boundary.
vi.mock("next/dynamic", () => ({
  default: () => {
    const Noop = () => null;
    return Noop;
  },
}));

// The left dock slot — chat by default, swapped to tasks/detail. Titles also
// render in the always-mounted Inbox panel, so assertions scope to it.
const leftPanel = () => within(screen.getByTestId("demo-left-panel"));

describe("WorkspaceDemo", () => {
  it("renders the canned roster, the three dock panels, and the Tasks view", () => {
    render(<WorkspaceDemo />);
    // Sidebar roster from demoData.
    for (const name of ["Friday", "Atlas", "Stitch", "Tally"]) {
      expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    }
    // Dashboard + Inbox panels are mounted alongside the chat.
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Inbox" })).toBeInTheDocument();

    // Sidebar "Tasks" row swaps the left slot to the Tasks view.
    fireEvent.click(screen.getAllByRole("button", { name: "Tasks" })[0]);
    const left = leftPanel();
    expect(left.getByText("Research the market")).toBeInTheDocument();
    expect(left.getByText("Name it and take the handle")).toBeInTheDocument();
    expect(left.getByText("Find the first fifty buyers")).toBeInTheDocument();
    expect(left.getByText("Price it against the market")).toBeInTheDocument();
  });

  it("inbox tabs and search filter honestly, with the real empty state", () => {
    render(<WorkspaceDemo />);
    // Mine: every demo task is agent-assigned, so the visitor's tab is empty.
    fireEvent.click(screen.getByRole("tab", { name: "Mine" }));
    expect(screen.getByText("Inbox zero.")).toBeInTheDocument();
    // Unmatched search on All shows the search empty state.
    fireEvent.click(screen.getByRole("tab", { name: "All" }));
    fireEvent.change(screen.getByLabelText("Search inbox"), {
      target: { value: "zzz-no-match" },
    });
    expect(screen.getByText("No matches.")).toBeInTheDocument();
    // A matching search renders the search-kind section header, not Today/Earlier.
    fireEvent.change(screen.getByLabelText("Search inbox"), {
      target: { value: "handle" },
    });
    expect(screen.getByText('Search results for "handle"')).toBeInTheDocument();
  });

  it("opens the task detail (real IssueHeader + IssueComments) on row click", () => {
    render(<WorkspaceDemo />);
    fireEvent.click(screen.getAllByRole("button", { name: "Tasks" })[0]);
    fireEvent.click(leftPanel().getByText("Research the market"));
    // Detail view: identifier, editable title header, and the canned comment.
    expect(leftPanel().getAllByText("NWC-1").length).toBeGreaterThan(0);
    expect(leftPanel().getByText(/Research is in/)).toBeInTheDocument();
    // Back returns to the list.
    fireEvent.click(screen.getByRole("button", { name: "← Back" }));
    expect(leftPanel().getByText("Find the first fifty buyers")).toBeInTheDocument();
  });
});
