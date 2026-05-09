import { describe, test, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueProperties } from "@/components/teams/issues/IssueProperties";
import type {
  Issue,
  CompanyAgent,
  IssueProject,
} from "@/components/teams/shared/types";

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
    render(<IssueProperties issue={baseIssue} onAssigneeChange={vi.fn()} onProjectChange={vi.fn()} />);
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Priority")).toBeInTheDocument();
    expect(screen.getByText("Assignee")).toBeInTheDocument();
    expect(screen.getByText("Project")).toBeInTheDocument();
    expect(screen.getByText("Labels")).toBeInTheDocument();
    expect(screen.getByText("Created")).toBeInTheDocument();
    expect(screen.getByText("Updated")).toBeInTheDocument();
  });

  test("Assignee row renders an interactive picker trigger labeled 'Unassigned'", () => {
    render(<IssueProperties issue={baseIssue} onAssigneeChange={vi.fn()} onProjectChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Unassigned/i })).toBeInTheDocument();
  });

  test("Assignee row resolves the agent's name from the agents prop", () => {
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Friday" }];
    const issue: Issue = { ...baseIssue, assigneeAgentId: "ag_1" };
    render(
      <IssueProperties
        issue={issue}
        agents={agents}
        onAssigneeChange={vi.fn()}
        onProjectChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Friday/ })).toBeInTheDocument();
  });

  test("clicking an assignee option calls onAssigneeChange with the id", async () => {
    const user = userEvent.setup();
    const onAssigneeChange = vi.fn();
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Friday" }];
    render(
      <IssueProperties
        issue={baseIssue}
        agents={agents}
        onAssigneeChange={onAssigneeChange}
        onProjectChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    await user.click(await screen.findByText("Friday"));
    expect(onAssigneeChange).toHaveBeenCalledWith("ag_1");
  });

  test("Project row renders an interactive picker trigger", () => {
    render(<IssueProperties issue={baseIssue} onAssigneeChange={vi.fn()} onProjectChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /No project/i })).toBeInTheDocument();
  });

  test("clicking a project option calls onProjectChange with the id", async () => {
    const user = userEvent.setup();
    const onProjectChange = vi.fn();
    const projects: IssueProject[] = [{ id: "p_1", name: "Inbox v1" }];
    render(
      <IssueProperties
        issue={baseIssue}
        projects={projects}
        onAssigneeChange={vi.fn()}
        onProjectChange={onProjectChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /No project/i }));
    await user.click(await screen.findByText("Inbox v1"));
    expect(onProjectChange).toHaveBeenCalledWith("p_1");
  });

  test("renders priority icon + label", () => {
    render(<IssueProperties issue={baseIssue} onAssigneeChange={vi.fn()} onProjectChange={vi.fn()} />);
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  test("renders '—' for null priority", () => {
    const issue: Issue = { ...baseIssue, priority: null };
    const { container } = render(
      <IssueProperties issue={issue} onAssigneeChange={vi.fn()} onProjectChange={vi.fn()} />,
    );
    const dashes = container.querySelectorAll('[data-empty="true"]');
    expect(Array.from(dashes).some((el) => el.textContent === "—")).toBe(true);
  });

  test("renders project name from issue.project when projects prop empty", () => {
    const issue: Issue = { ...baseIssue, project: { id: "p1", name: "Inbox v1" } };
    render(<IssueProperties issue={issue} onAssigneeChange={vi.fn()} onProjectChange={vi.fn()} />);
    // Trigger should label with the denormalized project name even when the
    // projects prop is empty (e.g. SWR not yet loaded).
    expect(screen.getByRole("button", { name: /Inbox v1/ })).toBeInTheDocument();
  });
});
