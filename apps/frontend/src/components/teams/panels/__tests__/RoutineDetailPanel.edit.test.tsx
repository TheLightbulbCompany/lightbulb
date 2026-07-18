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
    expect(screen.getByRole("button", { name: /edit routine/i })).toBeInTheDocument();
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
    post.mockResolvedValue({ secretMaterial: "whsec_new_123" });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /rotate secret/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/routines/r1/triggers/wh1/rotate-secret", {}),
    );
    expect(await screen.findByText("whsec_new_123")).toBeInTheDocument();
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

  it("saves an edit via PATCH, preserving the date-typed variable", async () => {
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit routine/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Title"), {
      target: { value: "Nightly digest v2" },
    });
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0];
    expect(path).toBe("/routines/r1");
    expect(body.title).toBe("Nightly digest v2");
    // The date variable round-trips unchanged (upstream #8655 type preserved).
    expect(body.variables).toEqual([
      { name: "asOf", type: "date", required: true },
    ]);
  });

  it("omits unchanged status on a title-only routine save", async () => {
    // The backend gates any body carrying status:"active" as a
    // fireability-increasing edit, so a benign rename must not re-send it.
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit routine/i }));
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

  it("surfaces a 402 gate message inline and keeps the dialog open", async () => {
    patch.mockRejectedValue({
      body: { detail: { message: "Scheduled routines are part of a paid plan." } },
    });
    const user = userEvent.setup();
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /edit routine/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /^Save$/ }));
    expect(
      await screen.findByText(/scheduled routines are part of a paid plan/i),
    ).toBeInTheDocument();
    // Dialog stays open on failure so the user can retry / adjust.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
