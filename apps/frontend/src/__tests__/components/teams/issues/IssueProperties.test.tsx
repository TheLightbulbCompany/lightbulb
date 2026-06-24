import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// IssueProperties now renders <IssueRelations>, which reads `GET /teams/issues`
// for blocker candidates. Mock the Teams API so the relation rows mount with an
// empty candidate list and never hit the network.
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import {
  IssueProperties,
  type IssuePropertiesProps,
} from "@/components/teams/issues/IssueProperties";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type {
  Issue,
  CompanyAgent,
  IssueProject,
} from "@/components/teams/shared/types";

beforeEach(() => {
  vi.mocked(useTeamsApi).mockReturnValue({
    read: vi.fn().mockReturnValue({ data: { issues: [] } }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
});

const baseIssue: Issue = {
  id: "iss_1",
  title: "Test",
  status: "todo",
  priority: "high",
  createdAt: "2026-05-04T00:00:00Z",
  updatedAt: "2026-05-05T12:00:00Z",
};

// The rail is the single editing surface for every field, so all four change
// handlers are required props. This helper supplies inert spies for the ones a
// given test does not care about.
function requiredHandlers(): Pick<
  IssuePropertiesProps,
  "onStatusChange" | "onPriorityChange" | "onAssigneeChange" | "onProjectChange" | "onBlockedByChange"
> {
  return {
    onStatusChange: vi.fn(),
    onPriorityChange: vi.fn(),
    onAssigneeChange: vi.fn(),
    onProjectChange: vi.fn(),
    onBlockedByChange: vi.fn(),
  };
}

describe("IssueProperties", () => {
  test("renders all 7 property rows", () => {
    render(<IssueProperties issue={baseIssue} {...requiredHandlers()} />);
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.getByText("Priority")).toBeInTheDocument();
    expect(screen.getByText("Assignee")).toBeInTheDocument();
    expect(screen.getByText("Project")).toBeInTheDocument();
    expect(screen.getByText("Labels")).toBeInTheDocument();
    expect(screen.getByText("Created")).toBeInTheDocument();
    expect(screen.getByText("Updated")).toBeInTheDocument();
  });

  test("Status row is an editable picker; choosing an option fires onStatusChange", async () => {
    const user = userEvent.setup();
    const onStatusChange = vi.fn();
    render(
      <IssueProperties
        issue={baseIssue}
        {...requiredHandlers()}
        onStatusChange={onStatusChange}
      />,
    );
    // The Status row trigger shows the current "Todo" label; opening it exposes
    // the full status list to pick a new value from.
    await user.click(screen.getByRole("button", { name: /Todo/ }));
    await user.click(await screen.findByText("In Progress"));
    expect(onStatusChange).toHaveBeenCalledWith("in_progress");
  });

  test("Priority row is an editable picker; choosing an option fires onPriorityChange", async () => {
    const user = userEvent.setup();
    const onPriorityChange = vi.fn();
    render(
      <IssueProperties
        issue={baseIssue}
        {...requiredHandlers()}
        onPriorityChange={onPriorityChange}
      />,
    );
    // Current priority "High" is the trigger label; pick "Low" from the popover.
    await user.click(screen.getByRole("button", { name: /High/ }));
    await user.click(await screen.findByText("Low"));
    expect(onPriorityChange).toHaveBeenCalledWith("low");
  });

  test("Assignee row renders an interactive picker trigger labeled 'Unassigned'", () => {
    render(<IssueProperties issue={baseIssue} {...requiredHandlers()} />);
    expect(screen.getByRole("button", { name: /Unassigned/i })).toBeInTheDocument();
  });

  test("Assignee row resolves the agent's name from the agents prop", () => {
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Friday" }];
    const issue: Issue = { ...baseIssue, assigneeAgentId: "ag_1" };
    render(
      <IssueProperties issue={issue} agents={agents} {...requiredHandlers()} />,
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
        {...requiredHandlers()}
        onAssigneeChange={onAssigneeChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    await user.click(await screen.findByText("Friday"));
    expect(onAssigneeChange).toHaveBeenCalledWith("ag_1");
  });

  test("Project row renders an interactive picker trigger", () => {
    render(<IssueProperties issue={baseIssue} {...requiredHandlers()} />);
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
        {...requiredHandlers()}
        onProjectChange={onProjectChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /No project/i }));
    await user.click(await screen.findByText("Inbox v1"));
    expect(onProjectChange).toHaveBeenCalledWith("p_1");
  });

  test("renders priority icon + label", () => {
    render(<IssueProperties issue={baseIssue} {...requiredHandlers()} />);
    // PriorityIcon (showLabel) renders the capitalized "High" label.
    expect(screen.getByText("High")).toBeInTheDocument();
  });

  test("renders '—' for null priority", () => {
    const issue: Issue = { ...baseIssue, priority: null };
    const { container } = render(
      <IssueProperties issue={issue} {...requiredHandlers()} />,
    );
    const dashes = container.querySelectorAll('[data-empty="true"]');
    expect(Array.from(dashes).some((el) => el.textContent === "—")).toBe(true);
  });

  test("renders project name from issue.project when projects prop empty", () => {
    const issue: Issue = { ...baseIssue, project: { id: "p1", name: "Inbox v1" } };
    render(<IssueProperties issue={issue} {...requiredHandlers()} />);
    // Trigger should label with the denormalized project name even when the
    // projects prop is empty (e.g. SWR not yet loaded).
    expect(screen.getByRole("button", { name: /Inbox v1/ })).toBeInTheDocument();
  });
});
