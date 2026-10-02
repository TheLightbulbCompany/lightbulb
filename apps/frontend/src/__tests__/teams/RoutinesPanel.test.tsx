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

const mockOpenMarketplace = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: mockOpenMarketplace }),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: mockPatch,
    del: mockDel,
  }),
}));

// T6: the "A connected app" rung talks directly to the app API (automations
// endpoints are mounted under /api/v1, not the Teams BFF), so it goes through
// useApi(), not useTeamsApi(). Preserve every other export (ApiError,
// BACKEND_URL, ...) via importOriginal so nothing else that imports @/lib/api
// breaks.
const mockAutomationsGet = vi.fn();
const mockAutomationsPost = vi.fn();
const mockAutomationsPut = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    useApi: () => ({
      get: mockAutomationsGet,
      post: mockAutomationsPost,
      put: mockAutomationsPut,
      patch: vi.fn(),
      del: vi.fn(),
    }),
  };
});

let connectionsData: unknown[] = [];
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({
    connections: connectionsData,
    isLoading: false,
    error: undefined,
    mutate: vi.fn(),
    connectPaste: vi.fn(),
    startOAuth: vi.fn(),
    disconnect: vi.fn(),
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

// The event catalog lives in a popover now (it used to be an always-open
// 260px list that pushed the rest of the form past the viewport edge), so a
// pick is: open the trigger, then choose the option.
async function openEventList(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /select (an )?events?…/i }));
}

describe("RoutinesPanel", () => {
  beforeEach(() => {
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
    mockRead.mockClear();
    mockAutomationsGet.mockReset();
    mockAutomationsPost.mockReset();
    mockAutomationsPut.mockReset();
    connectionsData = [];
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
  });

  it("with nothing built, offers both exits: build one here, or hire an agent from the marketplace", () => {
    // Canned automation is the marketplace's job (published agents carry their
    // routines). The empty state names both doors rather than growing a
    // preset catalog of its own.
    routinesData = { routines: [] };
    render(<RoutinesPanel />);
    expect(screen.getByText("No automations yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hire an agent" }));
    expect(mockOpenMarketplace).toHaveBeenCalledWith({ view: "browse" });
    fireEvent.click(screen.getByRole("button", { name: "Build one" }));
    expect(screen.getByRole("dialog", { name: /new automation/i })).toBeInTheDocument();
  });

  it("renders the routine title and a plain-English trigger sentence from the upstream shape", () => {
    render(<RoutinesPanel />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    // The row subtitle reads as a sentence ("on a schedule — ...") instead of
    // the raw cron expression — see triggerSentence.
    expect(screen.getByText(/on a schedule — Every day at 09:00/)).toBeInTheDocument();
  });

  it("prefers the webhook trigger over a maintenance schedule in the row sentence", () => {
    // Google-seeded automations carry a renewal schedule trigger alongside
    // their webhook; the row must still read as the webhook, regardless of
    // the array order the API returns.
    routinesData = {
      routines: [
        routine({
          title: "Google: Calendar changed",
          triggers: [
            {
              id: "tr-renew",
              kind: "schedule",
              enabled: true,
              cronExpression: "0 6 * * 1,4",
              label: "Keep the Google watch alive",
            },
            { id: "tr-hook", kind: "webhook", enabled: true },
          ],
        }),
      ],
    };
    render(<RoutinesPanel />);
    expect(screen.getByText(/when an app calls this webhook/)).toBeInTheDocument();
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
    expect(within(dialog).getByText("Run routine", { selector: "h2" })).toBeInTheDocument();
    expect(within(dialog).getByText("Daily standup")).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();

    // Fill the text variable; pick a select option (Radix Select portals).
    fireEvent.change(within(dialog).getByLabelText(/topic/i), {
      target: { value: "Q3 launch" },
    });
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "formal" }));

    await user.click(within(dialog).getByRole("button", { name: /^Run routine$/ }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    const [path, body] = mockPost.mock.calls[0];
    expect(path).toBe("/routines/rt1/run");
    const b = body as { source: string; variables: Record<string, unknown> };
    expect(b.source).toBe("manual");
    expect(b.variables).toEqual({ topic: "Q3 launch", tone: "formal" });
    // Ported stock dialog: the run carries the (prefilled) agent choice.
    expect(body).toHaveProperty("assigneeAgentId", "a1");
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
    const runBtn = within(dialog).getByRole("button", { name: /^Run routine$/ });
    expect(runBtn).toBeDisabled();
    await user.click(runBtn);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("create flow sends a native reviewer policy, then attaches the schedule trigger", async () => {
    const user = userEvent.setup();
    // 1st POST returns the new routine row; 2nd POST is the trigger.
    mockPost.mockResolvedValueOnce({ id: "rt2" }).mockResolvedValueOnce({
      trigger: { id: "tr2" },
    });
    render(<RoutinesPanel />);

    // "New automation" is both the masthead button and the dialog heading;
    // click the button (role) to open, then fill the fields.
    await user.click(screen.getByRole("button", { name: "New automation" }));
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Weekly review" },
    });
    // The schedule is a frequency + time picker now, not a cron field:
    // "Every week" on Monday at 10:00 is the "0 10 * * 1" this asserts below.
    fireEvent.change(screen.getByLabelText(/time of day/i), {
      target: { value: "10:00" },
    });
    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "Every week" }));
    // Pick an agent through the AssigneePicker (no more raw "Agent ID" input).
    // The picker is a Radix Popover → needs the full userEvent pointer sequence,
    // and its options portal to document.body. Both the toolbar filter and the
    // dialog field render an "Unassigned" picker, so scope to the dialog (the
    // Radix DialogContent rendered by EditorialDialog) before clicking.
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /unassigned/i }));
    await user.click(await screen.findByText("Friday"));
    await user.click(within(dialog).getByRole("button", { name: /no reviewer/i }));
    await user.click(await screen.findByText("Atlas"));
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
      "execution_policy",
    ]);
    for (const k of Object.keys(rb)) {
      expect(allowed.has(k)).toBe(true);
    }
    expect(rb.title).toBe("Weekly review");
    expect(rb.assignee_agent_id).toBe("a1");
    expect(rb.description).toBe("review last week");
    expect(rb.execution_policy).toMatchObject({
      mode: "normal",
      stages: [
        {
          type: "review",
          approvalsNeeded: 1,
          participants: [{ type: "agent", agentId: "a2", userId: null }],
        },
      ],
    });
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
        webhookUrl: "https://api.thelightbulb.company/hooks/abc",
        webhookSecret: "whsec_123",
      },
    });
    render(<RoutinesPanel />);

    await user.click(screen.getByRole("button", { name: "New automation" }));
    // The schedule field is the rung-picker's default; picking "Any other
    // app" swaps it out for the webhook fields.
    await user.click(screen.getByRole("radio", { name: /Any other app/ }));
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
    expect(await screen.findByText("https://api.thelightbulb.company/hooks/abc")).toBeInTheDocument();
    expect(screen.getByText("whsec_123")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(mockMutate).toHaveBeenCalled();
  });

  it("rung picker: roving tabindex + arrow keys move selection and focus (WAI-ARIA APG radiogroup)", async () => {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));

    // Rungs, in order: "A connected app", "Any other app", "On a schedule".
    // Default rung is "On a schedule" (the last) — the only tab stop; every
    // other rung is out of the tab order (roving tabindex).
    const connectedApp = screen.getByRole("radio", { name: /A connected app/ });
    const webhook = screen.getByRole("radio", { name: /Any other app/ });
    const schedule = screen.getByRole("radio", { name: /On a schedule/ });
    expect(schedule).toHaveAttribute("tabindex", "0");
    expect(connectedApp).toHaveAttribute("tabindex", "-1");

    // ArrowRight from the last rung wraps forward to the first.
    fireEvent.keyDown(schedule, { key: "ArrowRight" });
    expect(connectedApp).toHaveAttribute("aria-checked", "true");
    expect(connectedApp).toHaveAttribute("tabindex", "0");
    expect(schedule).toHaveAttribute("aria-checked", "false");
    expect(schedule).toHaveAttribute("tabindex", "-1");
    expect(document.activeElement).toBe(connectedApp);

    // ArrowDown continues forward to the middle rung.
    fireEvent.keyDown(connectedApp, { key: "ArrowDown" });
    expect(webhook).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(webhook);

    // ArrowUp moves backward.
    fireEvent.keyDown(webhook, { key: "ArrowUp" });
    expect(connectedApp).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(connectedApp);
  });
});

describe("RoutinesPanel — 'A connected app' rung (T6)", () => {
  function connection(overrides: Record<string, unknown> = {}) {
    return {
      provider: "stripe",
      display_name: "Stripe",
      acquisition: "direct_oauth",
      status: "connected",
      can_trigger: ["Invoice payment failed", "Charge refunded"],
      inbound: {
        webhook_registration: "guided_paste",
        events: [
          { id: "invoice.payment_failed", label: "Invoice payment failed" },
          { id: "charge.refunded", label: "Charge refunded" },
        ],
      },
      ...overrides,
    };
  }

  beforeEach(() => {
    mockAutomationsPost.mockResolvedValue({});
    mockAutomationsPut.mockResolvedValue({ status: "stored" });
  });

  async function openConnectedAppRung() {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    return user;
  }

  it("lists only connected providers that declare inbound events", async () => {
    connectionsData = [
      connection(),
      // Connected but no inbound events -> not trigger-capable.
      connection({ provider: "acme", display_name: "Acme", inbound: null, can_trigger: [] }),
      // Has inbound events but not connected -> not offered.
      connection({ provider: "slack", display_name: "Slack", status: "disconnected" }),
    ];
    const user = await openConnectedAppRung();

    await user.click(screen.getAllByRole("combobox")[0]);
    expect(await screen.findByRole("option", { name: "Stripe" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Acme" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Slack" })).not.toBeInTheDocument();
  });

  it("choosing a provider renders its inbound events in the searchable picker", async () => {
    connectionsData = [connection()];
    const user = await openConnectedAppRung();

    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);

    // The popover lists the events with a search input above.
    expect(await screen.findByRole("option", { name: "Invoice payment failed" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Charge refunded" })).toBeInTheDocument();
    expect(screen.getByLabelText("Search events")).toBeInTheDocument();
  });

  it("typing in the search input filters events by label and id; selecting a filtered event submits its id", async () => {
    connectionsData = [connection()];
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: true,
    });
    const user = await openConnectedAppRung();
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);

    // Label match narrows to the one refund event.
    await user.type(screen.getByLabelText("Search events"), "refund");
    expect(screen.getByRole("option", { name: "Charge refunded" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Invoice payment failed" })).not.toBeInTheDocument();

    // Id match works too — labels don't contain the raw id.
    await user.clear(screen.getByLabelText("Search events"));
    await user.type(screen.getByLabelText("Search events"), "invoice.payment");
    expect(screen.getByRole("option", { name: "Invoice payment failed" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Charge refunded" })).not.toBeInTheDocument();

    // Selecting the filtered event submits ITS id, not a positional index.
    await user.click(screen.getByRole("option", { name: "Invoice payment failed" }));
    await user.click(screen.getByText("Create"));
    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "stripe",
      event_ids: ["invoice.payment_failed"],
    });
  });

  it("a big catalog groups into Popular (the first three registry entries) then All events alphabetized", async () => {
    // 10 events (>8) triggers the grouping; registry order puts the curated
    // picks first, so they become the Popular pins.
    const events = [
      { id: "invoice.payment_failed", label: "Invoice payment failed" },
      { id: "charge.refunded", label: "Charge refunded" },
      { id: "customer.subscription.deleted", label: "Subscription canceled" },
      ...Array.from({ length: 7 }, (_, i) => ({
        id: `zz.event_${i}`,
        label: `Zz event ${i}`,
      })),
    ];
    connectionsData = [connection({ inbound: { webhook_registration: "guided_paste", events } })];
    const user = await openConnectedAppRung();
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);

    expect(screen.getByText("Popular")).toBeInTheDocument();
    expect(screen.getByText("All events")).toBeInTheDocument();
    // The popular pins come FIRST in DOM order, before the alphabetized rest.
    const options = screen.getAllByRole("option");
    expect(options.slice(0, 3).map((o) => o.textContent)).toEqual([
      "Invoice payment failed",
      "Charge refunded",
      "Subscription canceled",
    ]);
    expect(options).toHaveLength(10);

    // Filtering collapses the grouping to one flat matched list.
    await user.type(screen.getByLabelText("Search events"), "zz");
    expect(screen.queryByText("Popular")).not.toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(7);
  });

  it("guided (Stripe): submits {provider,event_ids}, then shows the proxy URL + a signing-secret field that PUTs it", async () => {
    connectionsData = [connection()];
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: true,
      instructions: "Paste this URL into your Stripe Dashboard webhook settings.",
      secret_hint: "whsec_…",
    });
    const user = await openConnectedAppRung();

    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Invoice payment failed" }));

    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "stripe",
      event_ids: ["invoice.payment_failed"],
    });

    expect(
      await screen.findByText("https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1"),
    ).toBeInTheDocument();

    // Done is gated until the whsec is saved — dismissing here would leave
    // an active, unsigned automation with no UI left to arm it.
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeDisabled();
    await user.click(doneButton);
    expect(mockMutate).not.toHaveBeenCalled();

    const secretField = screen.getByLabelText(/paste your stripe signing secret/i);
    expect(secretField).toHaveAttribute("type", "password");
    // Placeholder comes from the seed response's secret_hint — the
    // provider's own term for the secret, not a hardcoded Stripe-ism.
    expect(secretField).toHaveAttribute("placeholder", "whsec_…");
    await user.type(secretField, "whsec_abc123");
    await user.click(screen.getByRole("button", { name: /save secret/i }));

    await waitFor(() => expect(mockAutomationsPut).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPut).toHaveBeenCalledWith("/automations/pub_1/webhook-secret", {
      webhook_secret: "whsec_abc123",
    });

    // Once saved, Done is enabled and closes the dialog.
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
  });

  it("guided (Stripe): an instructions string embedding the proxy URL renders it as a wrapped code span, not raw inline text", async () => {
    connectionsData = [connection()];
    const proxyUrl =
      "https://api.thelightbulb.company/api/v1/hooks/t/org_3H3viGAfq2SXc9dIdcatmGXg8Td/b9b40b9a392b15fa77ddf1fa";
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: proxyUrl,
      guided: true,
      // Mirrors providers.yaml's real Stripe template, which splices the
      // literal proxy_url into the sentence — the shape that used to overflow
      // the dialog when rendered as one unbroken text node.
      instructions:
        `In your Stripe Dashboard, open Developers → Webhooks and add an endpoint ` +
        `pointed at ${proxyUrl} listening for the 'Charge refunded' event ` +
        `(charge.refunded). Stripe then shows the endpoint's signing secret — ` +
        `paste it back here to finish arming this automation.`,
      secret_hint: "whsec_…",
    });
    const user = await openConnectedAppRung();

    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Invoice payment failed" }));
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));

    // The URL is pulled out into its own break-all <code> span inside the
    // instructions paragraph — distinct from the SecretField "Webhook URL"
    // block above, which renders the same URL in its own <code>.
    await screen.findAllByText(proxyUrl);
    const urlCodes = screen.getAllByText(proxyUrl).filter((el) => el.tagName === "CODE");
    expect(urlCodes).toHaveLength(2);
    const instructionsCode = urlCodes.find((el) => el.className === "break-all");
    expect(instructionsCode).toBeDefined();

    // ...while the surrounding operational copy (event name + dashboard
    // path) is untouched — the fix must not drop or reword instructions.
    expect(
      screen.getByText(/open Developers → Webhooks and add an endpoint/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/'Charge refunded' event \(charge\.refunded\)/),
    ).toBeInTheDocument();
  });

  it("guided (Stripe): a whsec save failure renders inline in the finish dialog and stays retryable", async () => {
    connectionsData = [connection()];
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: true,
      instructions: "Paste this URL into your Stripe Dashboard webhook settings.",
    });
    mockAutomationsPut.mockRejectedValueOnce(new Error("Secret rejected by server"));
    const user = await openConnectedAppRung();

    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Invoice payment failed" }));
    await user.click(screen.getByText("Create"));
    await screen.findByText("https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1");

    const secretField = screen.getByLabelText(/paste your stripe signing secret/i);
    await user.type(secretField, "whsec_bad");
    await user.click(screen.getByRole("button", { name: /save secret/i }));

    // The failure is rendered INSIDE this finish dialog — previously it was
    // swallowed into `err` state that only the earlier (now-unmounted) form
    // step rendered, leaving the user stuck with no explanation and Done
    // still disabled.
    expect(
      await screen.findByText(/secret rejected by server/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Secret saved.")).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeDisabled();

    // Retryable: fixing the input and saving again succeeds and clears the
    // error.
    mockAutomationsPut.mockResolvedValueOnce({ status: "stored" });
    await user.click(screen.getByRole("button", { name: /save secret/i }));
    await waitFor(() => expect(mockAutomationsPut).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Secret saved.")).toBeInTheDocument();
    expect(screen.queryByText(/secret rejected by server/i)).not.toBeInTheDocument();
    expect(doneButton).toBeEnabled();
  });

  it("reverse-guided (PostHog): shows the URL + full Authorization header value, no paste step, Done immediately enabled", async () => {
    connectionsData = [
      connection({
        provider: "posthog",
        display_name: "PostHog",
        can_trigger: ["Exception captured"],
        inbound: {
          webhook_registration: "guided_paste",
          events: [{ id: "$exception", label: "Exception captured" }],
        },
      }),
    ];
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: true,
      instructions:
        "In PostHog, open Data pipelines → Destinations and create a new 'HTTP Webhook' destination.",
      secret_hint: null,
      webhook_secret: "pcsec_ph",
    });
    const user = await openConnectedAppRung();

    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "PostHog" }));
    await openEventList(user);
    await user.click(
      await screen.findByRole("option", { name: "Exception captured" }),
    );
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "posthog",
      event_ids: ["$exception"],
    });

    expect(
      await screen.findByText("https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1"),
    ).toBeInTheDocument();
    // The FULL header value, Bearer prefix included — copying it whole
    // eliminates the forgot-the-prefix failure.
    expect(screen.getByText("Bearer pcsec_ph")).toBeInTheDocument();
    expect(screen.getByText(/data pipelines/i)).toBeInTheDocument();

    // The secret flows Isol8 → PostHog: no paste-back input, no arming PUT,
    // and no "Registered." claim — the owner still has PostHog-side setup.
    expect(screen.queryByLabelText(/signing secret/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Registered.")).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
    expect(mockAutomationsPut).not.toHaveBeenCalled();
  });

  // Target selector: create a new automation (the unchanged default) vs arm
  // this event on one the owner already wrote.

  async function pickStripeRefundEvent() {
    connectionsData = [connection()];
    const user = await openConnectedAppRung();
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Charge refunded" }));
    return user;
  }

  it("defaults to creating a new automation, and submits no routine_id", async () => {
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_new",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: true,
    });
    const user = await pickStripeRefundEvent();

    // The picker sits on "Create a new automation" without being touched —
    // the one-click path is exactly as many clicks as it was.
    expect(screen.getByText("Create a new automation")).toBeInTheDocument();
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "stripe",
      event_ids: ["charge.refunded"],
    });
  });

  it("choosing an existing automation submits its routine_id and still runs the guided paste flow", async () => {
    routinesData = { routines: [routine({ id: "rt_revenue", title: "Revenue recovery" })] };
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_revenue",
      trigger_public_id: "pub_att",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_att",
      guided: true,
      instructions: "Paste this URL into your Stripe Dashboard webhook settings.",
      secret_hint: "whsec_…",
    });
    const user = await pickStripeRefundEvent();

    // [0] is the app picker; the target picker is the second (guided
    // providers have no resource dropdown between them).
    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByRole("option", { name: "Revenue recovery" }));
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "stripe",
      event_ids: ["charge.refunded"],
      routine_id: "rt_revenue",
    });

    // The guided half is unchanged — same URL to paste, same secret to arm
    // it with. Attaching only changes WHICH routine the trigger hangs off.
    expect(
      await screen.findByText("https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_att"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/paste your stripe signing secret/i)).toBeInTheDocument();
  });

  it("switching apps resets the target back to a new automation", async () => {
    routinesData = { routines: [routine({ id: "rt_revenue", title: "Revenue recovery" })] };
    connectionsData = [
      connection(),
      connection({
        provider: "posthog",
        display_name: "PostHog",
        inbound: {
          webhook_registration: "guided_paste",
          events: [{ id: "insight.alert", label: "Insight alert" }],
        },
      }),
    ];
    const user = await openConnectedAppRung();
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Stripe" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Charge refunded" }));
    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByRole("option", { name: "Revenue recovery" }));
    // Scope to the dialog — the routine also names itself in the list behind it.
    expect(
      within(screen.getByRole("dialog")).getByText("Revenue recovery"),
    ).toBeInTheDocument();

    // A different app may not even support attaching — the target can't
    // survive the switch.
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "PostHog" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Insight alert" }));
    expect(screen.getByText("Create a new automation")).toBeInTheDocument();
  });

});

describe("RoutinesPanel — auto_github repo picker", () => {
  function githubConnection() {
    return {
      provider: "github",
      display_name: "GitHub",
      acquisition: "direct_oauth",
      status: "connected",
      can_trigger: ["Pull request opened or changed"],
      inbound: {
        webhook_registration: "auto_github",
        events: [
          { id: "pull_request", label: "Pull request opened or changed" },
          { id: "push", label: "Code pushed" },
        ],
      },
    };
  }

  beforeEach(() => {
    mockAutomationsGet.mockReset();
    mockAutomationsPost.mockReset();
    mockAutomationsPut.mockReset();
    mockMutate.mockReset();
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
    connectionsData = [githubConnection()];
    mockAutomationsGet.mockResolvedValue([
      { id: "acme/site", label: "acme/site" },
      { id: "acme/api", label: "acme/api" },
    ]);
  });

  async function pickGithubEvent() {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "GitHub" }));
    await openEventList(user);
    await user.click(
      await screen.findByRole("option", { name: "Pull request opened or changed" }),
    );
    return user;
  }

  it("fetches the repo list once GitHub is selected and offers it after the event pick", async () => {
    const user = await pickGithubEvent();

    await waitFor(() =>
      expect(mockAutomationsGet).toHaveBeenCalledWith(
        "/automations/providers/github/resources",
      ),
    );
    // The third dropdown appears only after the event is chosen; Create stays
    // gated until a repo is picked.
    expect(screen.getByText("Repository")).toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeDisabled();

    await user.click(screen.getAllByRole("combobox")[1]);
    expect(await screen.findByRole("option", { name: "acme/site" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "acme/api" })).toBeInTheDocument();
  });

  it("submits {provider, event_ids, resource} and shows the live-on-repo success state", async () => {
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: false,
      instructions: null,
      secret_hint: null,
      resource: "acme/site",
    });
    const user = await pickGithubEvent();

    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByRole("option", { name: "acme/site" }));
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "github",
      event_ids: ["pull_request"],
      resource: "acme/site",
    });

    // Auto path: already armed — no paste step, Done is immediately enabled.
    expect(
      await screen.findByText(/GitHub registered the webhook on acme\/site — this automation is live\./),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/signing secret/i)).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
  });

  it("renders the repo-list failure inline instead of a dead dropdown", async () => {
    mockAutomationsGet.mockRejectedValue(new Error("GitHub is not connected"));
    await pickGithubEvent();

    expect(await screen.findByText(/github is not connected/i)).toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeDisabled();
  });
});

describe("RoutinesPanel — auto_vercel project picker", () => {
  function vercelConnection() {
    return {
      provider: "vercel_cli",
      display_name: "Vercel",
      acquisition: "paste_token",
      status: "connected",
      can_trigger: ["Deployment failed"],
      inbound: {
        webhook_registration: "auto_vercel",
        events: [
          { id: "deployment.error", label: "Deployment failed" },
          { id: "deployment.succeeded", label: "Deployment succeeded" },
        ],
      },
    };
  }

  beforeEach(() => {
    mockAutomationsGet.mockReset();
    mockAutomationsPost.mockReset();
    mockAutomationsPut.mockReset();
    mockMutate.mockReset();
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
    connectionsData = [vercelConnection()];
    mockAutomationsGet.mockResolvedValue([
      { id: "prj_1", label: "my-site" },
      { id: "prj_2", label: "my-api" },
    ]);
  });

  it("fetches projects from the parameterized route, submits the project id, and shows the live success state", async () => {
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: false,
      instructions: null,
      secret_hint: null,
      resource: "prj_1",
    });
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Vercel" }));
    await openEventList(user);
    await user.click(
      await screen.findByRole("option", { name: "Deployment failed" }),
    );

    // The picker feed comes from the provider-parameterized resources route.
    await waitFor(() =>
      expect(mockAutomationsGet).toHaveBeenCalledWith(
        "/automations/providers/vercel_cli/resources",
      ),
    );
    // The third dropdown is labeled by the provider's resource noun; Create
    // stays gated until a project is picked.
    expect(screen.getByText("Project")).toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeDisabled();

    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByRole("option", { name: "my-site" }));
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    // The resource in the body is the project ID (what the backend hooks),
    // not the display name.
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "vercel_cli",
      event_ids: ["deployment.error"],
      resource: "prj_1",
    });

    // Auto path: already armed — no paste step, Done is immediately enabled.
    expect(
      // The success copy names the PICKED project by its human label, not the
      // prj_… id the backend echoes — the picker already knows the name.
      await screen.findByText(/Vercel registered the webhook on my-site — this automation is live\./),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/signing secret/i)).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
  });
});

describe("RoutinesPanel — auto_meta asset picker", () => {
  // auto_meta is the one registration shape TWO providers share, and its
  // resource is a different thing in each — so the noun map needs a
  // per-provider override, which is what these two cases pin.
  function metaConnection(provider: string, displayName: string, events: { id: string; label: string }[]) {
    return {
      provider,
      display_name: displayName,
      acquisition: "direct_oauth",
      status: "connected",
      can_trigger: [events[0].label],
      inbound: { webhook_registration: "auto_meta", events },
    };
  }

  beforeEach(() => {
    mockAutomationsGet.mockReset();
    mockAutomationsPost.mockReset();
    mockAutomationsPut.mockReset();
    mockMutate.mockReset();
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
  });

  it("labels the Meta Ads picker 'Page' and submits the Page id", async () => {
    connectionsData = [
      metaConnection("meta_ads", "Meta Ads", [
        { id: "leadgen", label: "New lead" },
        { id: "feed", label: "Page post activity" },
      ]),
    ];
    mockAutomationsGet.mockResolvedValue([
      { id: "1112223334", label: "Acme Page" },
    ]);
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: false,
      instructions: null,
      secret_hint: null,
      // The backend echoes the asset NAME here (a numeric Page id names
      // nothing to the owner), so the success copy uses it as-is.
      resource: "Acme Page",
    });
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Meta Ads" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "New lead" }));

    await waitFor(() =>
      expect(mockAutomationsGet).toHaveBeenCalledWith(
        "/automations/providers/meta_ads/resources",
      ),
    );
    expect(screen.getByText("Page")).toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeDisabled();

    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(await screen.findByRole("option", { name: "Acme Page" }));
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith(
      "/automations/from-integration",
      { provider: "meta_ads", event_ids: ["leadgen"], resource: "1112223334" },
    );
    expect(
      await screen.findByText(
        /Meta Ads registered the webhook on Acme Page — this automation is live\./,
      ),
    ).toBeInTheDocument();
  });

  it("labels the Instagram picker 'Instagram account'", async () => {
    connectionsData = [
      metaConnection("instagram", "Instagram", [
        { id: "comments", label: "New comment" },
        { id: "messages", label: "New DM" },
      ]),
    ];
    mockAutomationsGet.mockResolvedValue([
      { id: "7778889990", label: "acme.co" },
    ]);
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Instagram" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "New comment" }));

    await waitFor(() =>
      expect(mockAutomationsGet).toHaveBeenCalledWith(
        "/automations/providers/instagram/resources",
      ),
    );
    // Same shape as Meta Ads, different noun — the per-provider override.
    expect(screen.getByText("Instagram account")).toBeInTheDocument();
    expect(screen.queryByText("Page")).not.toBeInTheDocument();
  });
});

describe("RoutinesPanel — auto_google_watch account picker", () => {
  function googleConnection() {
    return {
      provider: "google",
      display_name: "Google Workspace",
      acquisition: "direct_oauth",
      status: "connected",
      can_trigger: ["Calendar changed"],
      inbound: {
        webhook_registration: "auto_google_watch",
        events: [
          { id: "calendar.changed", label: "Calendar changed" },
          { id: "drive.changed", label: "Drive file changed" },
        ],
      },
    };
  }

  beforeEach(() => {
    mockAutomationsGet.mockReset();
    mockAutomationsPost.mockReset();
    mockAutomationsPut.mockReset();
    mockMutate.mockReset();
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
    connectionsData = [googleConnection()];
    // The picker feed is the owner's connected account emails — the email is
    // both the id and the label.
    mockAutomationsGet.mockResolvedValue([
      { id: "ada@example.com", label: "ada@example.com" },
    ]);
  });

  it("fetches accounts from the parameterized route, submits the email, and shows the live success state", async () => {
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: false,
      instructions: null,
      secret_hint: null,
      resource: "ada@example.com",
    });
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(
      await screen.findByRole("option", { name: "Google Workspace" }),
    );
    await openEventList(user);
    await user.click(
      await screen.findByRole("option", { name: "Calendar changed" }),
    );

    // The picker feed comes from the provider-parameterized resources route.
    await waitFor(() =>
      expect(mockAutomationsGet).toHaveBeenCalledWith(
        "/automations/providers/google/resources",
      ),
    );
    // The third dropdown is labeled by the provider's resource noun; Create
    // stays gated until an account is picked.
    expect(screen.getByText("Google account")).toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeDisabled();

    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(
      await screen.findByRole("option", { name: "ada@example.com" }),
    );
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    // The resource in the body is the account email (what the backend
    // validates against the connected-accounts inventory).
    expect(mockAutomationsPost).toHaveBeenCalledWith("/automations/from-integration", {
      provider: "google",
      event_ids: ["calendar.changed"],
      resource: "ada@example.com",
    });

    // Auto path: already armed — no paste step, Done is immediately enabled,
    // and the success copy names the account (emails are human-readable as-is).
    expect(
      await screen.findByText(/Google Workspace registered the webhook on ada@example\.com — this automation is live\./),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/signing secret/i)).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
  });

  it("offers the attach target, and states what arming the watch will add", async () => {
    // A Google watch stays alive only because the routine's own agent re-arms
    // it, which needs a renewal clause in the routine's instructions. That
    // used to make attaching impossible (it would have overwritten a
    // hand-written body) — the clause is now APPENDED as a delimited block,
    // so the target is offered and the consequence is stated up front.
    routinesData = { routines: [routine({ id: "rt_revenue", title: "Revenue recovery" })] };
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByRole("option", { name: "Google Workspace" }));
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: "Calendar changed" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Create a new automation")).toBeInTheDocument();
    // No disclosure until an existing automation is actually the target.
    expect(within(dialog).queryByText(/appended to the END/i)).not.toBeInTheDocument();

    // app, Google account, then the attach target — take the last, rather than
    // an index the account picker shifts.
    const combos = within(dialog).getAllByRole("combobox");
    await user.click(combos[combos.length - 1]);
    await user.click(await screen.findByRole("option", { name: "Revenue recovery" }));

    const disclosure = await within(dialog).findByText(/renewal step appended to the END/i);
    expect(disclosure).toHaveTextContent(/twice-weekly schedule trigger/i);
    expect(disclosure).toHaveTextContent(/Everything you wrote stays exactly as it is/i);
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
    await user.click(screen.getByRole("button", { name: /sort automations/i }));
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

describe("RoutinesPanel — gmail per-event registration (poll / owner_setup)", () => {
  // The google connection with the gmail events: the block level stays
  // auto_google_watch (which is what renders the account picker), while each
  // gmail event carries its own `registration` override.
  function gmailGoogleConnection() {
    return {
      provider: "google",
      display_name: "Google Workspace",
      acquisition: "direct_oauth",
      status: "connected",
      can_trigger: [
        "Calendar changed",
        "New email (checked on a schedule)",
        "New email (real-time push)",
      ],
      inbound: {
        webhook_registration: "auto_google_watch",
        events: [
          { id: "calendar.changed", label: "Calendar changed", registration: null },
          {
            id: "gmail.poll",
            label: "New email (checked on a schedule)",
            registration: "poll",
          },
          {
            id: "gmail.message",
            label: "New email (real-time push)",
            registration: "owner_setup",
          },
        ],
      },
    };
  }

  beforeEach(() => {
    mockAutomationsGet.mockReset();
    mockAutomationsPost.mockReset();
    mockAutomationsPut.mockReset();
    mockMutate.mockReset();
    routinesData = { routines: [routine()] };
    agentsData = { agents: AGENTS };
    connectionsData = [gmailGoogleConnection()];
    mockAutomationsGet.mockResolvedValue([
      { id: "ada@example.com", label: "ada@example.com" },
    ]);
  });

  async function pickGmailEvent(label: string) {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: "New automation" }));
    await user.click(screen.getByRole("radio", { name: /A connected app/ }));
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(
      await screen.findByRole("option", { name: "Google Workspace" }),
    );
    await openEventList(user);
    await user.click(await screen.findByRole("option", { name: label }));
    // Account picker — the block-level auto shape's resource, common to both
    // gmail events.
    await waitFor(() =>
      expect(mockAutomationsGet).toHaveBeenCalledWith(
        "/automations/providers/google/resources",
      ),
    );
    await user.click(screen.getAllByRole("combobox")[1]);
    await user.click(
      await screen.findByRole("option", { name: "ada@example.com" }),
    );
    return user;
  }

  it("gmail.poll: no topic input, submits without one, and the success copy reads live-on-a-schedule with no URL", async () => {
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: null,
      proxy_url: null,
      guided: false,
      instructions: null,
      secret_hint: null,
      resource: "ada@example.com",
      poll: true,
    });
    const user = await pickGmailEvent("New email (checked on a schedule)");

    // The poll shape needs no Pub/Sub topic — the extra input is
    // owner_setup-only.
    expect(screen.queryByText("Pub/Sub topic")).not.toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeEnabled();
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith(
      "/automations/from-integration",
      {
        provider: "google",
        event_ids: ["gmail.poll"],
        resource: "ada@example.com",
      },
    );

    // Schedule-backed success copy — nothing to wire, no webhook URL field.
    expect(screen.getByText("Automation registered")).toBeInTheDocument();
    expect(
      await screen.findByText(
        /Checks ada@example\.com's inbox every 10 minutes — this automation is live\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Webhook URL")).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
  });

  it("gmail.message: requires the Pub/Sub topic, submits it, shows the setup instructions, and Done is not gated", async () => {
    mockAutomationsPost.mockResolvedValue({
      routine_id: "rt_1",
      trigger_public_id: "pub_1",
      proxy_url: "https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1",
      guided: false,
      instructions:
        "Create the topic, grant gmail-api-push the Publisher role, then add a push subscription.",
      secret_hint: null,
      resource: "ada@example.com",
    });
    const user = await pickGmailEvent("New email (real-time push)");

    // The owner_setup shape adds the topic input; Create stays gated until
    // it's filled in.
    expect(screen.getByText("Pub/Sub topic")).toBeInTheDocument();
    expect(screen.getByText("Create").closest("button")).toBeDisabled();

    fireEvent.change(
      screen.getByPlaceholderText("projects/my-project/topics/isol8-gmail"),
      { target: { value: "projects/my-project/topics/isol8-gmail" } },
    );
    await user.click(screen.getByText("Create"));

    await waitFor(() => expect(mockAutomationsPost).toHaveBeenCalledTimes(1));
    expect(mockAutomationsPost).toHaveBeenCalledWith(
      "/automations/from-integration",
      {
        provider: "google",
        event_ids: ["gmail.message"],
        resource: "ada@example.com",
        topic: "projects/my-project/topics/isol8-gmail",
      },
    );

    // Finish dialog: URL + one-time setup instructions, informational only —
    // no secret paste-back, and Done is enabled immediately (unlike guided).
    expect(await screen.findByText("Finish setup")).toBeInTheDocument();
    expect(
      screen.getByText("https://api-dev.thelightbulb.company/api/v1/hooks/t/owner_1/pub_1"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Create the topic, grant gmail-api-push/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/signing secret/i)).not.toBeInTheDocument();
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton).toBeEnabled();
    await user.click(doneButton);
    expect(mockMutate).toHaveBeenCalled();
  });
});
