import { describe, test, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { IssueProperties } from "@/components/teams/issues/IssueProperties";
import type { Issue, CompanyAgent } from "@/components/teams/shared/types";

const baseIssue: Issue = {
  id: "iss_1",
  title: "Test",
  status: "todo",
  priority: "high",
  createdAt: "2026-05-04T00:00:00Z",
  updatedAt: "2026-05-05T12:00:00Z",
};

describe("IssueProperties", () => {
  test("renders all 7 property rows", () => {
    render(<IssueProperties issue={baseIssue} />);
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Priority")).toBeInTheDocument();
    expect(screen.getByText("Assignee")).toBeInTheDocument();
    expect(screen.getByText("Project")).toBeInTheDocument();
    expect(screen.getByText("Labels")).toBeInTheDocument();
    expect(screen.getByText("Created")).toBeInTheDocument();
    expect(screen.getByText("Updated")).toBeInTheDocument();
  });

  test("renders 'Unassigned' when no assigneeAgentId", () => {
    render(<IssueProperties issue={baseIssue} />);
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  test("looks up agent name from agents list", () => {
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Main Agent" }];
    const issue: Issue = { ...baseIssue, assigneeAgentId: "ag_1" };
    render(<IssueProperties issue={issue} agents={agents} />);
    expect(screen.getByText("Main Agent")).toBeInTheDocument();
  });

  test("renders 'Unknown agent' when assignee id has no match", () => {
    const issue: Issue = { ...baseIssue, assigneeAgentId: "ag_missing" };
    render(<IssueProperties issue={issue} agents={[]} />);
    expect(screen.getByText("Unknown agent")).toBeInTheDocument();
  });

  test("renders priority icon + label", () => {
    render(<IssueProperties issue={baseIssue} />);
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  test("renders '—' for null priority", () => {
    const issue: Issue = { ...baseIssue, priority: null };
    const { container } = render(<IssueProperties issue={issue} />);
    // Editorial reskin: muted dashes are tagged via data-empty rather than a
    // tailwind utility class.
    const dashes = container.querySelectorAll('[data-empty="true"]');
    expect(Array.from(dashes).some((el) => el.textContent === "—")).toBe(true);
  });

  test("renders project name from issue.project", () => {
    const issue: Issue = { ...baseIssue, project: { id: "p1", name: "Inbox v1" } };
    render(<IssueProperties issue={issue} />);
    expect(screen.getByText("Inbox v1")).toBeInTheDocument();
  });

  test("renders labels as chips", () => {
    const issue: Issue = {
      ...baseIssue,
      labels: [
        { id: "l1", name: "bug" },
        { id: "l2", name: "p1" },
      ],
    };
    render(<IssueProperties issue={issue} />);
    expect(screen.getByText("bug")).toBeInTheDocument();
    expect(screen.getByText("p1")).toBeInTheDocument();
  });

  test("renders relative times for created + updated", () => {
    render(<IssueProperties issue={baseIssue} />);
    const timeEls = document.querySelectorAll("time[datetime]");
    expect(timeEls.length).toBeGreaterThanOrEqual(2);
  });
});
