import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SWRResponse } from "swr";
import { RoutineDetailPanel } from "../RoutineDetailPanel";

// Full useTeamsApi mock: reads are per-path stubs; writes are spies the tests
// assert against. Mirrors the read-only RoutineDetailPanel.test.tsx pattern but
// wires post/patch/del so the A2 edit affordances can be exercised.
type ReadStub = Partial<SWRResponse<unknown, Error>>;
let reads: Record<string, ReadStub> = {};
const post = vi.fn();
const patch = vi.fn();
const del = vi.fn();
const mutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined, mutate },
    post,
    patch,
    del,
  }),
}));

// Set-signing-secret goes through /api/v1 (useApi), not the Teams BFF above.
// Keep the module's other exports (notably ApiError, which notifyConversionGate
// — used by this component's `run` helper — imports from "@/lib/api") intact
// via importOriginal.
const apiPut = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    useApi: () => ({
      get: vi.fn(),
      post: vi.fn(),
      put: apiPut,
      del: vi.fn(),
    }),
  };
});

function seedReads(overrides: Record<string, ReadStub> = {}) {
  reads = {
    "/routines/r1": {
      data: {
        id: "r1",
        title: "Nightly digest",
        // Every stored variable is referenced as {{name}} in the template —
        // Paperclip's sync drops any that isn't, so this is the only shape a
        // persisted routine can actually have.
        description: "Summarize activity as of {{asOf}}.",
        status: "active",
        priority: "medium",
        assigneeAgentId: "ag_1",
        triggers: [
          { id: "t1", kind: "schedule", cronExpression: "0 9 * * *", enabled: true },
        ],
        variables: [{ name: "asOf", type: "date", required: true }],
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-02T00:00:00Z",
      },
      isLoading: false,
      error: undefined,
      mutate,
    },
    "/routines/r1/runs": { data: { runs: [] }, isLoading: false, error: undefined, mutate },
    "/routines/r1/revisions": {
      data: {
        revisions: [
          { id: "rev_2", revisionNumber: 2, changeSummary: "Updated routine", createdAt: "2026-01-02T00:00:00Z" },
          { id: "rev_1", revisionNumber: 1, changeSummary: "Created routine", createdAt: "2026-01-01T00:00:00Z" },
        ],
      },
      isLoading: false,
      error: undefined,
      mutate,
    },
    "/agents": {
      data: {
        agents: [
          { id: "ag_1", name: "Friday" },
          { id: "ag_2", name: "Atlas" },
          { id: "ag_3", name: "Gauge" },
          { id: "ag_4", name: "Tally" },
        ],
      },
      isLoading: false,
      error: undefined,
      mutate,
    },
    ...overrides,
  };
}

beforeEach(() => {
  seedReads();
  vi.clearAllMocks();
  post.mockResolvedValue({});
  patch.mockResolvedValue({});
  del.mockResolvedValue({});
  apiPut.mockResolvedValue({});
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

// The schedule is authored through the human picker now (frequency + time),
// not a raw cron field — "0 9 * * *" -> "30 8 * * 1" in the picker's terms.
async function pickWeeklyMondayAt0830(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
) {
  fireEvent.change(within(dialog).getByLabelText(/time of day/i), {
    target: { value: "08:30" },
  });
  await user.click(within(dialog).getByRole("combobox"));
  await user.click(await screen.findByRole("option", { name: "Every week" }));
}

describe("RoutineDetailPanel — editable (A2)", () => {
  it("keeps the read-only view and exposes Edit + Add affordances", () => {
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByRole("heading", { name: "Nightly digest" })).toBeInTheDocument();
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /edit automation/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add trigger/i })).toBeInTheDocument();
  });

  it("adds a schedule trigger via POST /routines/{id}/triggers", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /add trigger/i }));
    const dialog = await screen.findByRole("dialog");
    // Cron defaults to "0 9 * * *"; just submit.
    await user.click(within(dialog).getByRole("button", { name: /^Add$/ }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/routines/r1/triggers");
    expect(body).toMatchObject({ kind: "schedule", cron_expression: "0 9 * * *" });
  });

  it("deletes a trigger via DELETE after confirm", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /delete every day at 09:00/i }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("/routines/r1/triggers/t1"));
  });

  it("rotates a webhook trigger secret and shows the material once", async () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Hook routine",
          status: "active",
          triggers: [
            { id: "wh1", kind: "webhook", signingMode: "hmac_sha256", publicId: "pub_1", enabled: true },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    // The real wire shape is an OBJECT (RoutineTriggerSecretMaterial), not a
    // bare string — a string mock here would hide the "Objects are not valid
    // as a React child" crash that a naive `<code>{rotatedSecret}</code>`
    // render produces.
    post.mockResolvedValue({
      secretMaterial: {
        webhookUrl: "https://api.flickmeon.ai/hooks/pub_1",
        webhookSecret: "whsec_new_123",
      },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /rotate to a new random secret/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/routines/r1/triggers/wh1/rotate-secret", {}),
    );
    // Renders via SecretField (label + copyable secret value) without throwing.
    expect(await screen.findByText("Webhook secret")).toBeInTheDocument();
    expect(await screen.findByText("whsec_new_123")).toBeInTheDocument();
  });

  it("sets a signing secret to a specific value via PUT /automations/{publicId}/webhook-secret", async () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Hook routine",
          status: "active",
          triggers: [
            { id: "wh1", kind: "webhook", signingMode: "hmac_sha256", publicId: "pub_1", enabled: true },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /set signing secret/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Signing secret"), "whsec_stripe_abc");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(apiPut).toHaveBeenCalledWith("/automations/pub_1/webhook-secret", {
        webhook_secret: "whsec_stripe_abc",
      }),
    );
  });

  it("hides the rotate button for an unsigned webhook (signingMode none)", async () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Hook routine",
          status: "active",
          triggers: [
            { id: "wh1", kind: "webhook", signingMode: "none", publicId: "pub_1", enabled: true },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.queryByRole("button", { name: /rotate to a new random secret/i })).not.toBeInTheDocument();
  });

  // Arming an EXISTING webhook trigger means pasting its fire URL into the
  // provider's dashboard. The guided create-flow shows that URL once; without
  // it on the trigger projection here, a hand-written routine could only be
  // armed through the API (#1888). The value is server-owned — asserted as the
  // exact string the BFF projected, never one this panel rebuilt.
  it("shows each webhook trigger's server-projected fire URL with a copy affordance", () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Revenue recovery",
          status: "active",
          triggers: [
            {
              id: "wh1",
              kind: "webhook",
              label: "Stripe invoice.payment_failed",
              signingMode: "hmac_sha256",
              publicId: "pub_1",
              fireUrl: "https://api.flickmeon.ai/api/v1/hooks/t/org_1/pub_1",
              enabled: true,
            },
            { id: "t1", kind: "schedule", cronExpression: "0 9 * * *", enabled: true },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(
      screen.getByText("https://api.flickmeon.ai/api/v1/hooks/t/org_1/pub_1"),
    ).toBeInTheDocument();
    // Exactly one — the schedule trigger has no fire URL.
    expect(screen.getAllByText("Fire URL")).toHaveLength(1);
    expect(
      screen.getByRole("button", {
        name: "Copy fire URL for Stripe invoice.payment_failed",
      }),
    ).toBeInTheDocument();
  });

  // The motivating case: one routine, several webhook triggers (one per provider
  // event). Every URL differs only by public id, and each row's copy button is
  // otherwise labelled identically — so without `copyLabel` a screen-reader user
  // gets N buttons all reading "Copy Fire URL" and no way to tell which event
  // they are arming. Same reason the rotate/delete controls name their trigger.
  it("gives each webhook trigger's copy button a label naming that trigger", () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Revenue recovery",
          status: "active",
          triggers: [
            {
              id: "wh1",
              kind: "webhook",
              label: "Stripe invoice.payment_failed",
              signingMode: "hmac_sha256",
              publicId: "pub_1",
              fireUrl: "https://api.flickmeon.ai/api/v1/hooks/t/org_1/pub_1",
              enabled: true,
            },
            {
              id: "wh2",
              kind: "webhook",
              label: "Stripe charge.dispute.created",
              signingMode: "hmac_sha256",
              publicId: "pub_2",
              fireUrl: "https://api.flickmeon.ai/api/v1/hooks/t/org_1/pub_2",
              enabled: true,
            },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getAllByText("Fire URL")).toHaveLength(2);
    // Both URLs rendered, and each copy button is uniquely addressable.
    expect(
      screen.getByText("https://api.flickmeon.ai/api/v1/hooks/t/org_1/pub_1"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("https://api.flickmeon.ai/api/v1/hooks/t/org_1/pub_2"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy fire URL for Stripe invoice.payment_failed" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Copy fire URL for Stripe charge.dispute.created" }),
    ).toBeInTheDocument();
  });

  // Negative branch of the server-side guard, seen from the client: a webhook
  // trigger the BFF did not stamp (no publicId) renders no Fire URL field at all
  // rather than an empty or half-built one.
  it("renders no fire URL for a webhook trigger the server did not stamp", () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Hook routine",
          status: "active",
          triggers: [
            { id: "wh1", kind: "webhook", signingMode: "hmac_sha256", publicId: null, enabled: true },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.queryByText("Fire URL")).not.toBeInTheDocument();
  });

  it("renders revisions and restores one via POST", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText("Updated routine")).toBeInTheDocument();
    const restoreButtons = screen.getAllByRole("button", { name: /restore revision/i });
    await user.click(restoreButtons[0]);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/routines/r1/revisions/rev_2/restore", {}),
    );
  });

  it("sends edited variables via PATCH, preserving the date type", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Title"), {
      target: { value: "Nightly digest v2" },
    });
    // Actually edit the variable (give it a label) so the array is dirty and
    // must be sent. Its `date` type must survive the round-trip (upstream #8655).
    fireEvent.change(within(dialog).getByPlaceholderText("label (optional)"), {
      target: { value: "As of date" },
    });
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1");
    expect(body.title).toBe("Nightly digest v2");
    // The date variable round-trips with its type + new label preserved.
    expect(body.variables).toEqual([
      { name: "asOf", type: "date", required: true, label: "As of date" },
    ]);
  });

  it("omits variables on a title-only save when the variables are unchanged", async () => {
    // A stale loaded detail must NOT clobber newer variable definitions: if the
    // user only renames the routine, the (possibly stale) variables array must
    // not be re-sent — the backend would otherwise replace the newer defs.
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Title"), {
      target: { value: "Nightly digest v2" },
    });
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1");
    expect(body.title).toBe("Nightly digest v2");
    // Variables were never touched → omitted entirely.
    expect(body).not.toHaveProperty("variables");
  });

  it("omits unchanged status on a title-only routine save", async () => {
    // The backend gates any body carrying status:"active" as a
    // fireability-increasing edit, so a benign rename must not re-send it.
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Title"), {
      target: { value: "Nightly digest renamed" },
    });
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1");
    expect(body.title).toBe("Nightly digest renamed");
    expect(body).not.toHaveProperty("status");
    expect(body).not.toHaveProperty("priority");
    expect(body).not.toHaveProperty("execution_policy");
  });

  it("edits the stored reviewer while preserving unrelated policy fields", async () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Reviewed digest",
          status: "active",
          assigneeAgentId: "ag_1",
          executionPolicy: {
            mode: "normal",
            commentRequired: false,
            stages: [
              {
                id: "stage-review",
                type: "review",
                approvalsNeeded: 1,
                participants: [
                  {
                    id: "participant-review",
                    type: "agent",
                    agentId: "ag_2",
                    userId: null,
                  },
                ],
              },
              {
                id: "stage-approval",
                type: "approval",
                approvalsNeeded: 1,
                participants: [
                  {
                    id: "participant-approval",
                    type: "agent",
                    agentId: "ag_4",
                    userId: null,
                  },
                ],
              },
            ],
          },
          triggers: [],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Atlas" }));
    await user.click(await screen.findByText("Gauge"));
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1");
    expect(body).toEqual({
      execution_policy: expect.objectContaining({
        commentRequired: false,
        stages: [
          expect.objectContaining({
            id: "stage-review",
            type: "review",
            participants: [expect.objectContaining({ agentId: "ag_3" })],
          }),
          {
            id: "stage-approval",
            type: "approval",
            approvalsNeeded: 1,
            participants: [
              {
                id: "participant-approval",
                type: "agent",
                agentId: "ag_4",
                userId: null,
              },
            ],
          },
        ],
      }),
    });
  });

  it("sends explicit null when the reviewer is removed from a reviewer-only policy", async () => {
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Reviewed digest",
          status: "active",
          assigneeAgentId: "ag_1",
          executionPolicy: {
            mode: "normal",
            stages: [
              {
                id: "stage-review",
                type: "review",
                approvalsNeeded: 1,
                participants: [
                  {
                    id: "participant-review",
                    type: "agent",
                    agentId: "ag_2",
                    userId: null,
                  },
                ],
              },
            ],
          },
          triggers: [],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Atlas" }));
    await user.click(await screen.findByRole("button", { name: "No reviewer" }));
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/routines/r1", {
        execution_policy: null,
      }),
    );
  });

  it("sends only { enabled: false } when a trigger is disabled without other edits", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit every day at 09:00/i }));
    const dialog = await screen.findByRole("dialog");
    // Uncheck Enabled and save nothing else — cron/timezone stay untouched.
    await user.click(within(dialog).getByLabelText("Enabled"));
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/routines/r1/triggers/t1", { enabled: false }),
    );
  });

  it("sends cron_expression (and not timezone) when the schedule actually changes", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit every day at 09:00/i }));
    const dialog = await screen.findByRole("dialog");
    await pickWeeklyMondayAt0830(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1/triggers/t1");
    expect(body.cron_expression).toBe("30 8 * * 1");
    expect(body).not.toHaveProperty("timezone");
  });

  it("seeds UTC (not the browser zone) when editing a trigger with no stored timezone, and omits it when only cron changes", async () => {
    // Force a non-UTC browser zone. The stored trigger (t1) has no timezone, so the
    // field must seed the backend's real default (UTC) — NOT the browser zone. If it
    // seeded the browser zone, a user outside UTC editing only the cron would see
    // local time in the field, have it match the (browser-tz) baseline, and silently
    // omit `timezone` from the patch, leaving the schedule on UTC while the UI implied
    // local time — firing at the wrong local hour.
    const tzSpy = vi
      .spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions")
      .mockReturnValue({ timeZone: "America/New_York" } as Intl.ResolvedDateTimeFormatOptions);
    try {
      const user = userEvent.setup();
      render(<RoutineDetailPanel routineId="r1" />);
      await user.click(screen.getByRole("button", { name: /edit every day at 09:00/i }));
      const dialog = await screen.findByRole("dialog");
      // Honest field: shows what actually fires (UTC), not the browser zone.
      expect(within(dialog).getByDisplayValue("UTC")).toBeInTheDocument();
      expect(within(dialog).queryByDisplayValue("America/New_York")).toBeNull();

      // Editing only the cron leaves timezone unchanged (UTC) → it must NOT be sent,
      // so the schedule stays on the UTC the field displayed (no silent tz drift).
      await pickWeeklyMondayAt0830(user, dialog);
      await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
      await waitFor(() => expect(patch).toHaveBeenCalled());
      const [path, body] = patch.mock.calls[0];
      expect(path).toBe("/routines/r1/triggers/t1");
      expect(body.cron_expression).toBe("30 8 * * 1");
      expect(body).not.toHaveProperty("timezone");
    } finally {
      tzSpy.mockRestore();
    }
  });

  it("sends label: null (not a dropped field) when a previously-set trigger label is cleared", async () => {
    // Blanking a label that was set must reach the backend as an explicit null.
    // If it went as `undefined`, JSON.stringify would drop it and the clear would
    // be silently lost — the old label would persist after refresh.
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Nightly digest",
          status: "active",
          triggers: [
            {
              id: "t1",
              kind: "schedule",
              cronExpression: "0 9 * * *",
              timezone: "UTC",
              label: "Morning digest",
              enabled: true,
            },
          ],
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit morning digest/i }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByLabelText(/label \(optional\)/i));
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1/triggers/t1");
    // Explicit null survives JSON.stringify; a dropped/undefined field would not.
    expect(Object.prototype.hasOwnProperty.call(body, "label")).toBe(true);
    expect(body.label).toBeNull();
    // Untouched fields are still omitted (partial PATCH stays partial).
    expect(body).not.toHaveProperty("cron_expression");
    expect(body).not.toHaveProperty("enabled");
  });

  it("surfaces a 402 gate message inline and keeps the dialog open", async () => {
    patch.mockRejectedValue({
      body: { detail: { message: "Scheduled routines are part of a paid plan." } },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(
      await screen.findByText(/scheduled routines are part of a paid plan/i),
    ).toBeInTheDocument();
    // Dialog stays open on failure so the user can retry / adjust.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

// Paperclip re-derives a routine's variables from its template on every update,
// keeping only the definitions referenced as {{NAME}}. A definition the text
// never mentions is dropped behind a 200 — the dialog has to say so up front.
describe("EditRoutineDialog — variable validation", () => {
  async function openEditorAndAddVar(name: string) {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /add variable/i }));
    const nameInputs = within(dialog).getAllByPlaceholderText("name");
    fireEvent.change(nameInputs[nameInputs.length - 1], { target: { value: name } });
    return { user, dialog };
  }

  it("blocks Save and names the unreferenced placeholder", async () => {
    const { user, dialog } = await openEditorAndAddVar("GTM_GOAL_ID");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /\{\{GTM_GOAL_ID\}\} is not used in the title or description/,
    );
    const save = within(dialog).getByRole("button", { name: /^Save$/ });
    expect(save).toBeDisabled();
    await user.click(save);
    expect(patch).not.toHaveBeenCalled();
  });

  it("saves once the variable is referenced in the description", async () => {
    const { user, dialog } = await openEditorAndAddVar("GTM_GOAL_ID");
    fireEvent.change(within(dialog).getByLabelText("Description"), {
      target: { value: "Summarize as of {{asOf}} against {{GTM_GOAL_ID}}." },
    });
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [, body] = patch.mock.calls[0];
    expect(body.variables).toEqual([
      { name: "asOf", type: "date", required: true },
      { name: "GTM_GOAL_ID", type: "text", required: true },
    ]);
  });

  it("accepts a markdown-escaped placeholder, matching upstream's matcher", async () => {
    // WYSIWYG markdown editors store `{{pr_url}}` as `{{pr\_url}}`; upstream
    // unescapes before matching, so the dialog must not flag it as unreferenced.
    const { dialog } = await openEditorAndAddVar("pr_url");
    fireEvent.change(within(dialog).getByLabelText("Description"), {
      target: { value: "Review {{asOf}} and {{pr\\_url}}." },
    });
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /^Save$/ })).toBeEnabled();
  });
});

// jsdom has no layout engine, so reachability is asserted on the structural
// contract that makes it viewport-independent: the dialog is height-bounded,
// the body is the only scroller, and the footer sits outside it.
describe.each([
  ["desktop 1280x720", 1280, 720],
  ["mobile 390x844", 390, 844],
])("EditRoutineDialog footer reachability — %s", (_label, width, height) => {
  beforeEach(() => {
    window.innerWidth = width;
    window.innerHeight = height;
    seedReads({
      "/routines/r1": {
        data: {
          id: "r1",
          title: "Wide digest",
          description: Array.from({ length: 7 }, (_, i) => `{{var_${i}}}`).join(" "),
          status: "active",
          priority: "medium",
          assigneeAgentId: "ag_1",
          triggers: [],
          variables: Array.from({ length: 7 }, (_, i) => ({
            name: `var_${i}`,
            type: "text",
            required: true,
          })),
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-02T00:00:00Z",
        },
        isLoading: false,
        error: undefined,
        mutate,
      },
    });
  });

  it("keeps Save outside the scrolling body with seven variable rows", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit automation/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByPlaceholderText("name")).toHaveLength(7);

    const body = dialog.querySelector("[data-dialog-body]");
    expect(body).toHaveClass("overflow-y-auto");
    expect(body).toHaveClass("min-h-0");
    // The dialog itself is height-capped, so the body — not the page — absorbs
    // the overflow.
    expect(dialog).toHaveClass("max-h-[85svh]");

    const save = within(dialog).getByRole("button", { name: /^Save$/ });
    expect(save).toBeEnabled();
    expect(body).not.toContainElement(save);
    expect(body).not.toContainElement(
      within(dialog).getByRole("button", { name: /^Cancel$/ }),
    );
  });
});
