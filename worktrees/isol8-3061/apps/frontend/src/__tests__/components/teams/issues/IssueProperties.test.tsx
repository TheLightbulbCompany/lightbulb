import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// IssueProperties renders <IssueRelations>, <LabelPicker>, and <ParentPicker>,
// which read `GET /teams/issues`, `/issues/labels`, and `/issues/{id}`
// respectively. A path-routing mock returns the right payload per fetched path
// so each control mounts with the candidate list it needs.
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import {
  IssueProperties,
  type IssuePropertiesProps,
} from "@/components/teams/issues/IssueProperties";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type {
  Issue,
  IssueLabel,
  CompanyAgent,
  IssueProject,
} from "@/components/teams/shared/types";

// Candidate issues for the parent/blocker pickers and labels for the label
// picker. Returned by the path-routing read mock below.
const CANDIDATE_ISSUES: Issue[] = [
  { id: "iss_2", identifier: "ISO-2", title: "A candidate parent", status: "todo" },
];
const COMPANY_LABELS: IssueLabel[] = [
  { id: "lbl_bug", name: "bug" },
  { id: "lbl_urgent", name: "urgent" },
];

type ReadResult<T> = { data: T | undefined };

function readFor<T>(path: string): ReadResult<T> {
  if (path === "/issues") {
    return { data: { issues: CANDIDATE_ISSUES } as unknown as T };
  }
  if (path === "/issues/labels") {
    return { data: { labels: COMPANY_LABELS } as unknown as T };
  }
  // Parent/Sub-tasks/{id} reads etc. — undefined is fine for these tests.
  return { data: undefined };
}

beforeEach(() => {
  vi.mocked(useTeamsApi).mockReturnValue({
    read: <T,>(path: string): ReadResult<T> => readFor<T>(path),
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

const baseMonitor = {
  nextCheckAt: "2026-07-01T09:00:00.000Z",
  notes: "watch the deploy",
  serviceName: "GitHub Actions",
};

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

  test("toggling a label option calls onLabelsChange with the full desired id set", async () => {
    const user = userEvent.setup();
    const onLabelsChange = vi.fn();
    // One label already applied; toggling a second sends BOTH ids (replace).
    const issue: Issue = { ...baseIssue, labels: [{ id: "lbl_bug", name: "bug" }] };
    render(
      <IssueProperties
        issue={issue}
        {...requiredHandlers()}
        onLabelsChange={onLabelsChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Add label/i }));
    await user.click(await screen.findByText("urgent"));
    expect(onLabelsChange).toHaveBeenCalledWith(["lbl_bug", "lbl_urgent"]);
  });

  test("selecting a parent calls onParentChange with the issue id", async () => {
    const user = userEvent.setup();
    const onParentChange = vi.fn();
    render(
      <IssueProperties
        issue={baseIssue}
        {...requiredHandlers()}
        onParentChange={onParentChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Set parent/i }));
    await user.click(await screen.findByText(/A candidate parent/));
    expect(onParentChange).toHaveBeenCalledWith("iss_2");
  });

  test("clearing the parent calls onParentChange with null", async () => {
    const user = userEvent.setup();
    const onParentChange = vi.fn();
    // An issue that already has a parent surfaces the "Change" trigger + the
    // "Clear parent" option in the popover.
    const issue: Issue = { ...baseIssue, parentId: "iss_99" };
    render(
      <IssueProperties
        issue={issue}
        {...requiredHandlers()}
        onParentChange={onParentChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Change/i }));
    await user.click(await screen.findByText("Clear parent"));
    expect(onParentChange).toHaveBeenCalledWith(null);
  });

  test("assigning a human member calls onAssigneeUserChange with the user id", async () => {
    const user = userEvent.setup();
    const onAssigneeUserChange = vi.fn();
    const members = [{ userId: "user_7", name: "Ada Lovelace", email: "ada@x.co" }];
    render(
      <IssueProperties
        issue={baseIssue}
        members={members}
        {...requiredHandlers()}
        onAssigneeUserChange={onAssigneeUserChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Unassigned/i }));
    // The popover lists agents + a People section; pick the human member.
    const option = await screen.findByText("Ada Lovelace");
    await user.click(option);
    expect(onAssigneeUserChange).toHaveBeenCalledWith("user_7");
  });

  // ── Monitor row ──────────────────────────────────────────────────────────
  // The detail page maps onMonitorChange → update(id,{executionPolicy:{monitor}}),
  // onMonitorClear → update(id,{executionPolicy:null}), and onCheckNow →
  // monitorCheckNow(id). The rail row owns the editor + actions; these tests
  // assert the row fires those handlers with the right payloads.

  test("Monitor row shows 'Not scheduled' when no monitor is set", () => {
    render(
      <IssueProperties
        issue={baseIssue}
        {...requiredHandlers()}
        onMonitorChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Monitor")).toBeInTheDocument();
    expect(screen.getByText("Not scheduled")).toBeInTheDocument();
  });

  test("scheduling a monitor fires onMonitorChange with the time + external service", async () => {
    const user = userEvent.setup();
    const onMonitorChange = vi.fn();
    render(
      <IssueProperties
        issue={baseIssue}
        {...requiredHandlers()}
        onMonitorChange={onMonitorChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Schedule/i }));
    // datetime-local takes a local "YYYY-MM-DDTHH:mm" value (no tz); the row
    // normalizes it to an ISO instant. Assert the external-service field flows
    // through verbatim and nextCheckAt is a non-empty ISO string (its exact
    // value depends on the runner timezone, so it is asserted by shape).
    await user.type(
      screen.getByLabelText("Re-check at"),
      "2026-07-01T09:00",
    );
    await user.type(
      screen.getByLabelText("External service"),
      "GitHub Actions",
    );
    // Both the trigger affordance and the editor submit read "Schedule"; scope
    // the submit click to the open popover dialog to disambiguate.
    const editor = screen.getByRole("dialog");
    await user.click(within(editor).getByRole("button", { name: /^Schedule$/i }));
    expect(onMonitorChange).toHaveBeenCalledTimes(1);
    const arg = onMonitorChange.mock.calls[0][0] as {
      nextCheckAt: string;
      serviceName: string | null;
    };
    expect(arg.serviceName).toBe("GitHub Actions");
    expect(typeof arg.nextCheckAt).toBe("string");
    expect(arg.nextCheckAt).not.toBe("");
    // Round-trips through Date → a valid ISO-8601 instant.
    expect(Number.isNaN(new Date(arg.nextCheckAt).getTime())).toBe(false);
  });

  test("a scheduled monitor renders the re-check time + service and a Clear action", () => {
    const onMonitorClear = vi.fn();
    const issue: Issue = {
      ...baseIssue,
      executionPolicy: { monitor: baseMonitor },
    };
    render(
      <IssueProperties
        issue={issue}
        {...requiredHandlers()}
        onMonitorChange={vi.fn()}
        onMonitorClear={onMonitorClear}
      />,
    );
    expect(screen.getByText(/Re-check/i)).toBeInTheDocument();
    expect(screen.getByText("GitHub Actions")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Clear$/i })).toBeInTheDocument();
  });

  test("clicking Clear fires onMonitorClear", async () => {
    const user = userEvent.setup();
    const onMonitorClear = vi.fn();
    const issue: Issue = {
      ...baseIssue,
      executionPolicy: { monitor: baseMonitor },
    };
    render(
      <IssueProperties
        issue={issue}
        {...requiredHandlers()}
        onMonitorChange={vi.fn()}
        onMonitorClear={onMonitorClear}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Clear$/i }));
    expect(onMonitorClear).toHaveBeenCalledTimes(1);
  });

  test("clicking 'Check now' invokes onCheckNow with the issue id", async () => {
    const user = userEvent.setup();
    const onCheckNow = vi.fn<(id: string) => Promise<void>>(() =>
      Promise.resolve(),
    );
    const issue: Issue = {
      ...baseIssue,
      executionPolicy: { monitor: baseMonitor },
    };
    render(
      <IssueProperties
        issue={issue}
        {...requiredHandlers()}
        onMonitorChange={vi.fn()}
        onCheckNow={onCheckNow}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Check now/i }));
    expect(onCheckNow).toHaveBeenCalledWith("iss_1");
  });

  // ── Reviewers / Approvers rows ─────────────────────────────────────────────
  // Each row is a multi-select over agents + members whose selection drives the
  // matching executionPolicy stage. The row hands the host the FULL desired
  // participant-value set (`agent:<id>` / `user:<id>`, replace semantics); the
  // host rebuilds the stage. These tests assert the row surfaces the right
  // values and fires onReviewersChange / onApproversChange with the full set.

  const reviewAgents: CompanyAgent[] = [
    { id: "ag_rev", name: "Reviewer Bot" },
  ];

  test("Reviewers row renders an 'Add reviewer' affordance when editable", () => {
    render(
      <IssueProperties
        issue={baseIssue}
        agents={reviewAgents}
        {...requiredHandlers()}
        onReviewersChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Reviewers")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Add reviewer/i }),
    ).toBeInTheDocument();
  });

  test("selecting a reviewer fires onReviewersChange with the agent value", async () => {
    const user = userEvent.setup();
    const onReviewersChange = vi.fn<(values: string[]) => void>();
    render(
      <IssueProperties
        issue={baseIssue}
        agents={reviewAgents}
        {...requiredHandlers()}
        onReviewersChange={onReviewersChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Add reviewer/i }));
    await user.click(await screen.findByText("Reviewer Bot"));
    expect(onReviewersChange).toHaveBeenCalledWith(["agent:ag_rev"]);
  });

  test("selecting an approver fires onApproversChange with the agent value", async () => {
    const user = userEvent.setup();
    const onApproversChange = vi.fn<(values: string[]) => void>();
    render(
      <IssueProperties
        issue={baseIssue}
        agents={reviewAgents}
        {...requiredHandlers()}
        onApproversChange={onApproversChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Add approver/i }));
    await user.click(await screen.findByText("Reviewer Bot"));
    expect(onApproversChange).toHaveBeenCalledWith(["agent:ag_rev"]);
  });

  test("Reviewers row resolves an existing stage participant into a chip", () => {
    const issue: Issue = {
      ...baseIssue,
      executionPolicy: {
        stages: [
          {
            type: "review",
            approvalsNeeded: 1,
            participants: [{ type: "agent", agentId: "ag_rev", userId: null }],
          },
        ],
      },
    };
    render(
      <IssueProperties
        issue={issue}
        agents={reviewAgents}
        {...requiredHandlers()}
        onReviewersChange={vi.fn()}
      />,
    );
    // The applied reviewer renders as a named chip (resolved off the agents
    // prop), proving stageParticipantValues reads the review stage correctly.
    expect(screen.getByText("Reviewer Bot")).toBeInTheDocument();
  });
});
