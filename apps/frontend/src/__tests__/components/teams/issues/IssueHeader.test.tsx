import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";
import { IssueHeader } from "@/components/teams/issues/IssueHeader";
import type { Issue } from "@/components/teams/shared/types";

const baseIssue: Issue = {
  id: "iss_1",
  identifier: "PAP-1",
  title: "Fix the inbox",
  status: "todo",
  priority: "high",
};

// The header is a title-only masthead now: status/priority/assignee moved to
// the right-rail Properties panel (single editing surface per field).
const makeProps = () => ({
  issue: baseIssue,
  onTitleSave: vi.fn().mockResolvedValue(undefined),
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssueHeader", () => {
  test("renders identifier + title only (no status/priority/assignee pickers)", () => {
    render(<IssueHeader {...makeProps()} />);
    expect(screen.getByText("PAP-1")).toBeInTheDocument();
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
    // Status/priority/assignee pickers were moved out of the header into the
    // Properties rail, so their labels must NOT render here.
    expect(screen.queryByText("Todo")).not.toBeInTheDocument();
    expect(screen.queryByText("High")).not.toBeInTheDocument();
  });

  test("renders without identifier when not provided", () => {
    const props = makeProps();
    const noIdIssue: Issue = { ...baseIssue, identifier: null };
    render(<IssueHeader {...props} issue={noIdIssue} />);
    expect(screen.queryByText("PAP-1")).not.toBeInTheDocument();
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
  });

  test("clicking title enters edit mode and focuses the input", () => {
    render(<IssueHeader {...makeProps()} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox");
    expect(input).toBeInTheDocument();
    expect(input).toHaveFocus();
  });

  test("Enter in edit mode saves title via onTitleSave", async () => {
    const props = makeProps();
    render(<IssueHeader {...props} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "New title" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(props.onTitleSave).toHaveBeenCalledWith("New title"),
    );
  });

  test("Enter trims whitespace before saving", async () => {
    const props = makeProps();
    render(<IssueHeader {...props} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  New title  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(props.onTitleSave).toHaveBeenCalledWith("New title"),
    );
  });

  test("Escape cancels edit and reverts to original title", () => {
    const props = makeProps();
    render(<IssueHeader {...props} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox");
    fireEvent.change(input, { target: { value: "Changed" } });
    fireEvent.keyDown(input, { key: "Escape" });
    // Back to display mode with original title.
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
    expect(props.onTitleSave).not.toHaveBeenCalled();
  });

  test("blur with same value does NOT fire onTitleSave", () => {
    const props = makeProps();
    render(<IssueHeader {...props} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox");
    fireEvent.blur(input);
    expect(props.onTitleSave).not.toHaveBeenCalled();
  });

  test("blur with empty / whitespace-only value does NOT fire (and reverts)", () => {
    const props = makeProps();
    render(<IssueHeader {...props} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox");
    fireEvent.change(input, { target: { value: "  " } });
    fireEvent.blur(input);
    expect(props.onTitleSave).not.toHaveBeenCalled();
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
  });

  test("blur with a changed value DOES fire onTitleSave", async () => {
    const props = makeProps();
    render(<IssueHeader {...props} />);
    fireEvent.click(screen.getByText("Fix the inbox"));
    const input = screen.getByDisplayValue("Fix the inbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Different" } });
    fireEvent.blur(input);
    await waitFor(() =>
      expect(props.onTitleSave).toHaveBeenCalledWith("Different"),
    );
  });

  test("draft resyncs when issue.title changes externally", () => {
    const props = makeProps();
    const { rerender } = render(<IssueHeader {...props} />);
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
    rerender(
      <IssueHeader
        {...props}
        issue={{ ...baseIssue, title: "Renamed externally" }}
      />,
    );
    expect(screen.getByText("Renamed externally")).toBeInTheDocument();
  });
});
