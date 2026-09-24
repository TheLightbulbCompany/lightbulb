// Tests for the stock-Paperclip routine ports: RoutineHistoryTab (revision
// list, preview, compare modal, restore) and RoutineRunVariablesDialog
// (agent requirement, required-variable gating, submit coercion).
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const post = vi.fn();
const mutate = vi.fn();
let revisions: unknown[] = [];

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { revisions }, isLoading: false, error: undefined, mutate }),
    post,
  }),
}));

const notifySuccess = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifySuccess: (...args: unknown[]) => notifySuccess(...args),
  notifyApiError: vi.fn(),
  notifyConversionGate: () => null,
}));

import { RoutineHistoryTab } from "../RoutineHistoryTab";
import { RoutineRunVariablesDialog } from "../RoutineRunVariablesDialog";

function revision(n: number, routine: Record<string, unknown>, triggers: unknown[] = []) {
  return {
    id: `rev_${n}`,
    revisionNumber: n,
    changeSummary: null,
    createdAt: "2026-09-01T00:00:00Z",
    createdByUserId: "u1",
    snapshot: {
      routine: {
        title: "Digest",
        priority: "medium",
        status: "active",
        assigneeAgentId: "ag_1",
        concurrencyPolicy: "coalesce_if_active",
        catchUpPolicy: "skip_missed",
        variables: [],
        env: null,
        description: "line one",
        ...routine,
      },
      triggers,
    },
  };
}

const agents = new Map([
  ["ag_1", { id: "ag_1", name: "Friday" }],
  ["ag_2", { id: "ag_2", name: "Atlas" }],
]);
const projects = new Map<string, { id: string; name: string }>();

describe("RoutineHistoryTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockResolvedValue({
      revision: { id: "rev_3", revisionNumber: 3 },
      restoredFromRevisionNumber: 1,
      secretMaterials: [],
    });
  });

  it("shows the empty state when only the bootstrap revision exists", () => {
    revisions = [revision(1, {})];
    render(
      <RoutineHistoryTab routine={{ id: "r1", title: "Digest" }} agents={agents} projects={projects} />,
    );
    expect(screen.getByText("No edits yet")).toBeInTheDocument();
  });

  it("previews a historical revision, flags differing fields, and compares with current", async () => {
    revisions = [
      revision(1, { title: "Old digest", assigneeAgentId: "ag_2", description: "line zero" }, [
        { id: "t1", kind: "schedule", cronExpression: "0 9 * * *", enabled: true },
      ]),
      revision(2, {}),
    ];
    const user = userEvent.setup();
    render(
      <RoutineHistoryTab
        routine={{ id: "r1", title: "Digest", latestRevisionId: "rev_2" }}
        agents={agents}
        projects={projects}
      />,
    );
    // Current revision is selected first; restoring it is disabled.
    expect(screen.getByRole("button", { name: "Restore this revision" })).toBeDisabled();

    await user.click(screen.getByTestId("revision-row-1"));
    expect(screen.getByText("Viewing revision 1 (read-only)")).toBeInTheDocument();
    expect(screen.getAllByText("differs from current").length).toBeGreaterThanOrEqual(2);

    await user.click(screen.getByRole("button", { name: /compare with current/i }));
    const dialog = await screen.findByRole("dialog", { name: /compare routine revisions/i });
    expect(within(dialog).getByText("Title")).toBeInTheDocument();
    expect(within(dialog).getByText("Old digest")).toBeInTheDocument();
    // Agent ids resolve to names; the removed trigger is listed.
    expect(within(dialog).getByText("Atlas")).toBeInTheDocument();
    expect(within(dialog).getByText("Trigger removed (schedule)")).toBeInTheDocument();
    expect(within(dialog).getByText("line zero")).toBeInTheDocument();
  });

  it("restores through the confirm dialog and reports the new revision", async () => {
    revisions = [revision(1, { title: "Old digest" }), revision(2, {})];
    const onRestored = vi.fn();
    const user = userEvent.setup();
    render(
      <RoutineHistoryTab
        routine={{ id: "r1", title: "Digest", latestRevisionId: "rev_2" }}
        agents={agents}
        projects={projects}
        onRestored={onRestored}
      />,
    );
    await user.click(screen.getByTestId("revision-row-1"));
    await user.click(screen.getByRole("button", { name: "Restore this revision" }));
    const dialog = await screen.findByRole("dialog", { name: /restore revision 1\?/i });
    await user.click(within(dialog).getByRole("button", { name: /restore as revision 3/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/routines/r1/revisions/rev_1/restore", {}),
    );
    expect(onRestored).toHaveBeenCalledWith(expect.objectContaining({ restoredFromRevisionNumber: 1 }));
    expect(notifySuccess).toHaveBeenCalledWith("Restored revision 1 as revision 3");
  });
});

describe("RoutineRunVariablesDialog", () => {
  const baseProps = {
    open: true,
    onOpenChange: vi.fn(),
    routineName: "Digest",
    projects: [],
    agents: [{ id: "ag_1", name: "Friday" }],
    isPending: false,
  };

  it("requires an agent before running", () => {
    render(
      <RoutineRunVariablesDialog {...baseProps} variables={[]} onSubmit={vi.fn()} />,
    );
    expect(screen.getByText("Default agent required for this run.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run routine" })).toBeDisabled();
  });

  it("gates on required variables, then coerces types and omits blanks on submit", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(
      <RoutineRunVariablesDialog
        {...baseProps}
        defaultAssigneeAgentId="ag_1"
        variables={[
          { name: "count", type: "number", required: true },
          { name: "note", type: "text", required: false },
          { name: "day", type: "date", required: false, defaultValue: "2026-09-01" },
        ]}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByText("Missing: count")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("count *"), { target: { value: "3" } });
    await user.click(screen.getByRole("button", { name: "Run routine" }));
    expect(onSubmit).toHaveBeenCalledWith({
      variables: { count: 3, day: "2026-09-01" },
      assigneeAgentId: "ag_1",
      projectId: null,
    });
  });
});
