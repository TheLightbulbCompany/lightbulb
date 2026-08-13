import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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

describe("WorkspaceDemo", () => {
  it("renders the canned roster and opens the Tasks view with the story's tasks", () => {
    render(<WorkspaceDemo />);
    // Sidebar roster from demoData.
    for (const name of ["Friday", "Atlas", "Stitch", "Tally"]) {
      expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    }
    fireEvent.click(screen.getByRole("button", { name: /tasks/i }));
    expect(screen.getByText("Research the market")).toBeInTheDocument();
    expect(screen.getByText("Name it and take the handle")).toBeInTheDocument();
    expect(screen.getByText("Find the first fifty buyers")).toBeInTheDocument();
    expect(screen.getByText("Price it against the market")).toBeInTheDocument();
  });

  it("opens the task detail (real IssueHeader + IssueComments) on row click", () => {
    render(<WorkspaceDemo />);
    fireEvent.click(screen.getByRole("button", { name: /tasks/i }));
    fireEvent.click(screen.getByText("Research the market"));
    // Detail view: identifier, editable title header, and the canned comment.
    expect(screen.getByText("NWC-1")).toBeInTheDocument();
    expect(screen.getByText(/Research is in/)).toBeInTheDocument();
    // Back returns to the list.
    fireEvent.click(screen.getByRole("button", { name: "← Back" }));
    expect(screen.getByText("Find the first fifty buyers")).toBeInTheDocument();
  });
});
