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

  it("renders the routine title and a plain-English trigger sentence from the upstream shape", () => {
    render(<RoutinesPanel />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    // The row subtitle reads as a sentence ("on a schedule — ...") instead of
    // the raw cron expression — see triggerSentence.
    expect(screen.getByText(/on a schedule — Every day at 09:00/)).toBeInTheDocument();
  });

  it("resolves the assignee id to the agent display name", () => {
    render(<RoutinesPanel />);
    // "Friday" (resolved name) shows in the row subtitle; the raw id "a1" does not.
    expect(screen.getByText(/Friday/)).toBeInTheDocument();
    expect(screen.queryByText(/agent a1/)).not.toBeInTheDocument();
  });

  it("shows the auto-paused row indicator only for a breaker-tripped routine, not a hand-pause", () => {
    routinesData = {
      routines: [
        routine({
          id: "rt-auto",
          title: "Breaker-tripped digest",
          status: "paused",
          autoPausedAt: "2026-08-10T08:00:00Z",
          consecutiveFailureCount: 4,
        }),
        routine({ id: "rt-hand", title: "Weekly report", status: "paused", autoPausedAt: null }),
      ],
    };
    render(<RoutinesPanel />);
    // Both rows render "Paused" via the toggle label; only the breaker-tripped
    // row also carries the distinct amber "auto-paused" affordance (titled, so
    // it's unambiguous from any incidental "auto-paused" substring elsewhere).
    expect(screen.getAllByTitle(/auto-paused after repeated failures/i)).toHaveLength(1);
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
    expect(screen.getByText(/on a schedule — Every day at 08:00/)).toBeInTheDocument();
    // Legacy agent_id resolves through the same roster map.
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
  });

  it("renders an Open link to the routine detail view", () => {
    render(<RoutinesPanel />);
    expect(screen.getByRole("link", { name: /open/i })).toHaveAttribute(
      "href",
      "/chat?panel=teams-routine-detail&detailId=rt1",
    );
  });

  it("renders an Active/Paused switch reflecting the routine status", () => {
    render(<RoutinesPanel />);
    // The row is status:"active" → switch is ON, label reads "Active".
    const sw = screen.getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("paused routine shows the switch OFF with a 'Paused' label", () => {
    routinesData = { routines: [routine({ status: "paused" })] };
    render(<RoutinesPanel />);
    const sw = screen.getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });

  it("toggle PATCHes {status: 'paused'} when disabling (no `enabled` field)", async () => {
    mockPatch.mockResolvedValue({});
    render(<RoutinesPanel />);

    // The active routine's switch flips it to paused.
    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledTimes(1);
    });
    // Upstream has no `enabled` field — enable/disable is a status flip.
    expect(mockPatch).toHaveBeenCalledWith("/routines/rt1", { status: "paused" });
    const [, body] = mockPatch.mock.calls[0];
    expect(Object.keys(body as Record<string, unknown>)).toEqual(["status"]);
  });

  it("toggle PATCHes {status: 'active'} when enabling a paused routine", async () => {
    mockPatch.mockResolvedValue({});
    routinesData = { routines: [routine({ status: "paused" })] };
    render(<RoutinesPanel />);

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledTimes(1);
    });
    expect(mockPatch).toHaveBeenCalledWith("/routines/rt1", { status: "active" });
  });

  it("runs a variable-less routine directly — no dialog, body is {source}", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValue({});
    render(<RoutinesPanel />);

    await user.click(screen.getByRole("button", { name: /run daily standup now/i }));

    // No editor opens for a routine with no declared variables.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    expect(mockPost).toHaveBeenCalledWith("/routines/rt1/run", {
      source: "manual",
    });
    // The one-click body carries no `variables` key.
    const [, body] = mockPost.mock.calls[0];
    expect(body).not.toHaveProperty("variables");
  });

  it("opens the variables editor for a routine that declares variables, and includes {variables} in the run body", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValue({});
    routinesData = {
      routines: [
        routine({
          variables: [
            { name: "topic", label: "Topic", type: "text", required: true },
            {
              name: "tone",
              type: "select",
              options: ["formal", "casual"],
              required: true,
            },
          ],
        }),
      ],
    };
    render(<RoutinesPanel />);

    // Clicking "Run now" opens the editor instead of firing immediately.
    await user.click(screen.getByRole("button", { name: /run daily standup now/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/run daily standup/i)).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();

    // Fill the text variable; pick a select option (Radix Select portals).
    fireEvent.change(within(dialog).getByLabelText(/topic/i), {
      target: { value: "Q3 launch" },
    });
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "formal" }));

    await user.click(within(dialog).getByRole("button", { name: /^Run$/ }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    const [path, body] = mockPost.mock.calls[0];
    expect(path).toBe("/routines/rt1/run");
    const b = body as { source: string; variables: Record<string, unknown> };
    expect(b.source).toBe("manual");
    expect(b.variables).toEqual({ topic: "Q3 launch", tone: "formal" });
  });

  it("blocks the run while a required variable is empty", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValue({});
    routinesData = {
      routines: [
        routine({
          variables: [{ name: "topic", type: "text", required: true }],
        }),
      ],
    };
    render(<RoutinesPanel />);

    await user.click(screen.getByRole("button", { name: /run daily standup now/i }));
    const dialog = await screen.findByRole("dialog");
    // Required + empty → Run is disabled, so clicking it does nothing.
    const runBtn = within(dialog).getByRole("button", { name: /^Run$/ });
    expect(runBtn).toBeDisabled();
    await user.click(runBtn);
    expect(mockPost).not.toHaveBeenCalled();
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
    // Substring match — the wrapping label also contains the cron hint text.
    fireEvent.change(screen.getByLabelText(/Schedule/), {
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

  it("'Any other app' rung: POSTs a webhook trigger and shows the one-time URL + secret", async () => {
    const user = userEvent.setup();
    mockPost.mockResolvedValueOnce({ id: "rt3" }).mockResolvedValueOnce({
      secretMaterial: {
        webhookUrl: "https://api.isol8.co/hooks/abc",
        webhookSecret: "whsec_123",
      },
    });
    render(<RoutinesPanel />);

    await user.click(screen.getByRole("button", { name: "New routine" }));
    // The schedule field is the rung-picker's default; picking "Any other
    // app" swaps it out for the webhook fields.
    await user.click(screen.getByRole("radio", { name: "Any other app" }));
    expect(screen.queryByLabelText(/^Schedule$/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Inbound orders" },
    });
    await user.click(screen.getByText("Create"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(2);
    });
    const [triggerPath, triggerBody] = mockPost.mock.calls[1];
    expect(triggerPath).toBe("/routines/rt3/triggers");
    const tb = triggerBody as Record<string, unknown>;
    expect(tb.kind).toBe("webhook");
    expect(tb.signing_mode).toBe("bearer");

    // The one-time secret dialog replaces the create form.
    expect(await screen.findByText("https://api.isol8.co/hooks/abc")).toBeInTheDocument();
    expect(screen.getByText("whsec_123")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(mockMutate).toHaveBeenCalled();
  });

  it("coming-soon rungs disable Create and show the inline affordance", async () => {
    const user = userEvent.setup();
    render(<RoutinesPanel />);

    await user.click(screen.getByRole("button", { name: "New routine" }));
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Whatever" },
    });
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));

    expect(screen.getByText(/Coming soon/)).toBeInTheDocument();
    expect(screen.getByText("Create")).toBeDisabled();
    await user.click(screen.getByText("Create"));
    expect(mockPost).not.toHaveBeenCalled();
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
