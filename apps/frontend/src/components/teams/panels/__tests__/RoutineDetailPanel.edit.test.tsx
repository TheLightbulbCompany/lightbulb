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

function seedReads(overrides: Record<string, ReadStub> = {}) {
  reads = {
    "/routines/r1": {
      data: {
        id: "r1",
        title: "Nightly digest",
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
      data: { agents: [{ id: "ag_1", name: "Friday" }] },
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
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

describe("RoutineDetailPanel — editable (A2)", () => {
  it("keeps the read-only view and exposes Edit + Add affordances", () => {
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByRole("heading", { name: "Nightly digest" })).toBeInTheDocument();
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /edit automation/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add schedule/i })).toBeInTheDocument();
  });

  it("adds a schedule trigger via POST /routines/{id}/triggers", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /add schedule/i }));
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
    await user.click(screen.getByRole("button", { name: /delete schedule/i }));
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
        webhookUrl: "https://api.isol8.co/hooks/pub_1",
        webhookSecret: "whsec_new_123",
      },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /rotate secret/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/routines/r1/triggers/wh1/rotate-secret", {}),
    );
    // Renders via SecretField (label + copyable secret value) without throwing.
    expect(await screen.findByText("Webhook secret")).toBeInTheDocument();
    expect(await screen.findByText("whsec_new_123")).toBeInTheDocument();
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
    expect(screen.queryByRole("button", { name: /rotate secret/i })).not.toBeInTheDocument();
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
  });

  it("sends only { enabled: false } when a trigger is disabled without other edits", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit schedule/i }));
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
    await user.click(screen.getByRole("button", { name: /edit schedule/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByPlaceholderText("0 9 * * *"), {
      target: { value: "30 8 * * 1" },
    });
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
      await user.click(screen.getByRole("button", { name: /edit schedule/i }));
      const dialog = await screen.findByRole("dialog");
      // Honest field: shows what actually fires (UTC), not the browser zone.
      expect(within(dialog).getByDisplayValue("UTC")).toBeInTheDocument();
      expect(within(dialog).queryByDisplayValue("America/New_York")).toBeNull();

      // Editing only the cron leaves timezone unchanged (UTC) → it must NOT be sent,
      // so the schedule stays on the UTC the field displayed (no silent tz drift).
      fireEvent.change(within(dialog).getByPlaceholderText("0 9 * * *"), {
        target: { value: "30 8 * * 1" },
      });
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
