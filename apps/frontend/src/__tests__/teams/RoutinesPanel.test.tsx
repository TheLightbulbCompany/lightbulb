// Tests for RoutinesPanel — covers the upstream RoutineListItem shape
// (title / assigneeAgentId / triggers[]), agent-name resolution, the standalone
// agent filter + sort controls, the per-row enable toggle (PATCH {status}),
// and the two-step create flow (POST routine → POST schedule trigger), matching
// upstream's create model where cron lives on a trigger, not the routine row.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();
const mockMutate = vi.fn();

// Per-path read mock: the panel reads BOTH `/routines` and `/agents`. Tests
// override `routinesData` / `agentsData` per suite; the mock discriminates on
// the path prefix so a single `read` impl serves both calls.
let routinesData: { routines: unknown[] };
let agentsData: { agents: unknown[] };

const mockRead = vi.fn((path: string) => {
  const data = path.startsWith("/agents") ? agentsData : routinesData;
  return { data, isLoading: false, error: null, mutate: mockMutate };
});

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: mockPatch,
    del: mockDel,
  }),
}));

import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";

// An upstream-shaped routine row (RoutineListItem subset).
function routine(overrides: Record<string, unknown> = {}) {
  return {
    id: "rt1",
    title: "Daily standup",
    assigneeAgentId: "a1",
    status: "active",
    triggers: [{ id: "tr1", kind: "schedule", enabled: true, cronExpression: "0 9 * * *" }],
    ...overrides,
  };
}

const AGENTS = [
  { id: "a1", name: "Friday" },
  { id: "a2", name: "Atlas" },
];

describe("RoutinesPanel", () => {
  beforeEach(() => {
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
    mockRead.mockClear();
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
  });

  it("renders the routine title and cron from the upstream shape", () => {
    render(<RoutinesPanel />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText(/0 9 \* \* \*/)).toBeInTheDocument();
  });

  it("resolves the assignee id to the agent display name", () => {
    render(<RoutinesPanel />);
    // "Friday" (resolved name) shows in the row subtitle; the raw id "a1" does not.
    expect(screen.getByText(/Friday/)).toBeInTheDocument();
    expect(screen.queryByText(/agent a1/)).not.toBeInTheDocument();
  });

  it("falls back to the legacy flat shape (name / cron / agent_id / enabled)", () => {
    routinesData = {
      routines: [
        {
          id: "rt9",
          name: "Legacy routine",
          cron: "0 8 * * *",
          agent_id: "a2",
          enabled: true,
        },
      ],
    };
    render(<RoutinesPanel />);
    expect(screen.getByText("Legacy routine")).toBeInTheDocument();
    expect(screen.getByText(/0 8 \* \* \*/)).toBeInTheDocument();
    // Legacy agent_id resolves through the same roster map.
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
  });

  it("renders an Open link to the routine detail page", () => {
    render(<RoutinesPanel />);
    expect(screen.getByRole("link", { name: /open/i })).toHaveAttribute(
      "href",
      "/teams/routines/rt1",
    );
  });

  it("toggle PATCHes {status: 'paused'} when disabling (no `enabled` field)", async () => {
    mockPatch.mockResolvedValue({});
    render(<RoutinesPanel />);

    fireEvent.click(screen.getByRole("checkbox"));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledTimes(1);
    });
    // Upstream has no `enabled` field — enable/disable is a status flip.
    expect(mockPatch).toHaveBeenCalledWith("/routines/rt1", { status: "paused" });
    const [, body] = mockPatch.mock.calls[0];
    expect(Object.keys(body as Record<string, unknown>)).toEqual(["status"]);
  });

  it("create flow: POST /routines (title/assignee/description) then POST the schedule trigger", async () => {
    const user = userEvent.setup();
    // 1st POST returns the new routine row; 2nd POST is the trigger.
    mockPost.mockResolvedValueOnce({ id: "rt2" }).mockResolvedValueOnce({
      trigger: { id: "tr2" },
    });
    render(<RoutinesPanel />);

    // "New routine" is both the masthead button and the dialog heading; click
    // the button (role) to open, then fill the fields.
    await user.click(screen.getByRole("button", { name: "New routine" }));
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Weekly review" },
    });
    fireEvent.change(screen.getByLabelText("Schedule"), {
      target: { value: "0 10 * * 1" },
    });
    // Pick an agent through the AssigneePicker (no more raw "Agent ID" input).
    // The picker is a Radix Popover → needs the full userEvent pointer sequence,
    // and its options portal to document.body. Both the toolbar filter and the
    // dialog field render an "Unassigned" picker, so scope to the dialog (the
    // Radix DialogContent rendered by EditorialDialog) before clicking.
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /unassigned/i }));
    await user.click(await screen.findByText("Friday"));
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "review last week" },
    });
    await user.click(screen.getByText("Create"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(2);
    });

    // 1st call: create the routine row — title/assignee/description/status only,
    // NO cron (that 422s upstream).
    const [routinePath, routineBody] = mockPost.mock.calls[0];
    expect(routinePath).toBe("/routines");
    const rb = routineBody as Record<string, unknown>;
    const allowed = new Set([
      "title",
      "assignee_agent_id",
      "description",
      "status",
      "priority",
      "project_id",
    ]);
    for (const k of Object.keys(rb)) {
      expect(allowed.has(k)).toBe(true);
    }
    expect(rb.title).toBe("Weekly review");
    expect(rb.assignee_agent_id).toBe("a1");
    expect(rb.description).toBe("review last week");
    expect(rb).not.toHaveProperty("cron");

    // 2nd call: attach the schedule trigger carrying the cron expression.
    const [triggerPath, triggerBody] = mockPost.mock.calls[1];
    expect(triggerPath).toBe("/routines/rt2/triggers");
    const tb = triggerBody as Record<string, unknown>;
    expect(tb.kind).toBe("schedule");
    expect(tb.cron_expression).toBe("0 10 * * 1");
    expect(tb.timezone).toBeTruthy();
  });
});

describe("RoutinesPanel sort", () => {
  beforeEach(() => {
    mockRead.mockClear();
    agentsData = { agents: AGENTS };
    routinesData = {
      routines: [
        routine({ id: "rt-b", title: "Beta", assigneeAgentId: "a1" }),
        routine({ id: "rt-a", title: "Alpha", assigneeAgentId: "a2" }),
      ],
    };
  });

  it("sorts by name ascending by default", () => {
    render(<RoutinesPanel />);
    const cells = screen.getAllByText(/Alpha|Beta/);
    // Alpha before Beta (default name-asc).
    expect(cells[0]).toHaveTextContent("Alpha");
    expect(cells[1]).toHaveTextContent("Beta");
  });

  it("flips direction when the active sort field is re-selected", async () => {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    // The sort menu is a Radix Popover → needs userEvent + portalled options.
    await user.click(screen.getByRole("button", { name: /sort routines/i }));
    // Re-select the already-active "Name" → flips to descending. The active
    // option's accessible name carries the direction arrow ("Name↑"), so match
    // on the prefix rather than an exact string.
    await user.click(await screen.findByRole("button", { name: /^Name/ }));
    const cells = screen.getAllByText(/Alpha|Beta/);
    expect(cells[0]).toHaveTextContent("Beta");
    expect(cells[1]).toHaveTextContent("Alpha");
  });
});

describe("RoutinesPanel agentId filter (inspector intrinsic scope)", () => {
  // A mixed-agent set: two for `a1`, one for `a2`. The inspector passes its
  // inspected agent's id; only that agent's routines should render. Unlike the
  // standalone `/teams` panel, the filter is intrinsic — NO dismissible chip /
  // toolbar picker.
  beforeEach(() => {
    mockRead.mockClear();
    agentsData = { agents: AGENTS };
    routinesData = {
      routines: [
        routine({ id: "rt1", title: "Daily standup", assigneeAgentId: "a1" }),
        routine({ id: "rt2", title: "Weekly review", assigneeAgentId: "a1" }),
        routine({ id: "rt3", title: "Nightly backup", assigneeAgentId: "a2" }),
      ],
    };
  });

  it("renders only the given agent's routines when agentId is set", () => {
    render(<RoutinesPanel agentId="a1" />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText("Weekly review")).toBeInTheDocument();
    expect(screen.queryByText("Nightly backup")).not.toBeInTheDocument();
  });

  it("renders all routines when agentId is absent (standalone unchanged)", () => {
    render(<RoutinesPanel />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText("Weekly review")).toBeInTheDocument();
    expect(screen.getByText("Nightly backup")).toBeInTheDocument();
  });

  it("does NOT render the toolbar agent picker when agentId is set", () => {
    render(<RoutinesPanel agentId="a1" />);
    // The standalone-only AssigneePicker trigger ("Unassigned") is absent in
    // intrinsic-scope mode.
    expect(
      screen.queryByRole("button", { name: /unassigned/i }),
    ).not.toBeInTheDocument();
  });
});
