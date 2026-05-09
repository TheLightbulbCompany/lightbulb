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

const makeProps = () => ({
  issue: baseIssue,
  onTitleSave: vi.fn().mockResolvedValue(undefined),
  onStatusChange: vi.fn().mockResolvedValue(undefined),
  onPriorityChange: vi.fn().mockResolvedValue(undefined),
  onAssigneeChange: vi.fn().mockResolvedValue(undefined),
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssueHeader", () => {
  test("renders identifier + title + status + priority icons", () => {
    render(<IssueHeader {...makeProps()} />);
    expect(screen.getByText("PAP-1")).toBeInTheDocument();
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
    // StatusIcon (showLabel) renders a "Todo" label; PriorityIcon (showLabel) renders "High".
    expect(screen.getByText("Todo")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
  });

  test("renders without identifier when not provided", () => {
    const props = makeProps();
    const noIdIssue: Issue = { ...baseIssue, identifier: null };
    render(<IssueHeader {...props} issue={noIdIssue} />);
    expect(screen.queryByText("PAP-1")).not.toBeInTheDocument();
    expect(screen.getByText("Fix the inbox")).toBeInTheDocument();
  });

  test("renders Medium label fallback when issue has no priority", () => {
    const props = makeProps();
    const noPriorityIssue: Issue = { ...baseIssue, priority: null };
    render(<IssueHeader {...props} issue={noPriorityIssue} />);
    expect(screen.getByText("Medium")).toBeInTheDocument();
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

  test("renders an inline AssigneePicker after status + priority", async () => {
    const onAssigneeChange = vi.fn();
    const agents = [{ id: "ag_1", name: "Friday" }];
    render(
      <IssueHeader
        issue={{ ...baseIssue, assigneeAgentId: "ag_1" }}
        agents={agents}
        onTitleSave={vi.fn()}
        onStatusChange={vi.fn()}
        onPriorityChange={vi.fn()}
        onAssigneeChange={onAssigneeChange}
      />,
    );
    // The trigger should render with the current assignee's name. Headers may
    // surface duplicate "Friday" labels (icon + visible label); a query for
    // the element by role+name confirms the picker chip is present.
    expect(
      screen.getAllByRole("button", { name: /Friday/ }).length,
    ).toBeGreaterThan(0);
  });
});
