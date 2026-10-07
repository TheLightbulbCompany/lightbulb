// apps/frontend/src/components/teams/panels/__tests__/TriggerDialog.webhook.test.tsx
//
// Task 2 (automations phase 1): in-panel webhook-trigger authoring. Tested via
// RoutineDetailPanel (TriggerDialog isn't independently exported) — mirrors
// the mock plumbing in RoutineDetailPanel.edit.test.tsx.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SWRResponse } from "swr";
import { RoutineDetailPanel } from "../RoutineDetailPanel";

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

function seedReads() {
  reads = {
    "/routines/r1": {
      data: {
        id: "r1",
        title: "Nightly digest",
        status: "active",
        priority: "medium",
        triggers: [],
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-02T00:00:00Z",
      },
      isLoading: false,
      error: undefined,
      mutate,
    },
    "/routines/r1/runs": { data: { runs: [] }, isLoading: false, error: undefined, mutate },
    "/routines/r1/revisions": { data: { revisions: [] }, isLoading: false, error: undefined, mutate },
    "/agents": { data: { agents: [] }, isLoading: false, error: undefined, mutate },
  };
}

beforeEach(() => {
  seedReads();
  vi.clearAllMocks();
  post.mockResolvedValue({});
  patch.mockResolvedValue({});
  del.mockResolvedValue({});
});

async function openAddDialogAsWebhook() {
  const user = userEvent.setup();
  render(<RoutineDetailPanel routineId="r1" />);
  await user.click(screen.getByRole("button", { name: /add trigger/i }));
  const dialog = await screen.findByRole("dialog");
  // "Any other app" is the raw signed-URL webhook rung — the same three-way
  // "what starts this?" picker the New-automation dialog shows, now that
  // Add-trigger offers every kind an automation can take.
  await user.click(within(dialog).getByRole("radio", { name: /any other app/i }));
  return { user, dialog };
}

describe("TriggerDialog — webhook branch", () => {
  it("shows a signing-mode select and replay-window input when kind=webhook", async () => {
    const { dialog } = await openAddDialogAsWebhook();
    expect(within(dialog).getByLabelText(/signing/i)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/replay window/i)).toBeInTheDocument();
    // Schedule-only fields must be gone.
    expect(within(dialog).queryByLabelText(/^schedule$/i)).not.toBeInTheDocument();
  });

  it("defaults signing mode to bearer and hides replay window for github_hmac/none", async () => {
    const { user, dialog } = await openAddDialogAsWebhook();
    const signing = within(dialog).getByLabelText(/signing/i) as HTMLSelectElement;
    expect(signing.value).toBe("bearer");
    expect(within(dialog).getByLabelText(/replay window/i)).toBeInTheDocument();

    await user.selectOptions(signing, "github_hmac");
    expect(within(dialog).queryByLabelText(/replay window/i)).not.toBeInTheDocument();

    await user.selectOptions(signing, "none");
    expect(within(dialog).queryByLabelText(/replay window/i)).not.toBeInTheDocument();

    await user.selectOptions(signing, "hmac_sha256");
    expect(within(dialog).getByLabelText(/replay window/i)).toBeInTheDocument();
  });

  it("posts kind/signing_mode/replay_window_sec on submit", async () => {
    const { user, dialog } = await openAddDialogAsWebhook();
    await user.click(within(dialog).getByRole("button", { name: /^Add$/ }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    const [path, body] = post.mock.calls[0];
    expect(path).toBe("/routines/r1/triggers");
    expect(body).toMatchObject({
      kind: "webhook",
      signing_mode: "bearer",
      replay_window_sec: 300,
    });
    expect(body).not.toHaveProperty("cron_expression");
  });

  it("shows the one-time webhookUrl + webhookSecret after create, with copy buttons", async () => {
    post.mockResolvedValue({
      trigger: { id: "wh_new", kind: "webhook" },
      secretMaterial: {
        webhookUrl: "https://paperclip.example/api/routine-triggers/public/pub_1/fire",
        webhookSecret: "whsec_abc123",
      },
    });
    const { user, dialog } = await openAddDialogAsWebhook();
    await user.click(within(dialog).getByRole("button", { name: /^Add$/ }));
    await waitFor(() => expect(post).toHaveBeenCalled());

    expect(
      await screen.findByText("https://paperclip.example/api/routine-triggers/public/pub_1/fire"),
    ).toBeInTheDocument();
    expect(screen.getByText("whsec_abc123")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy webhook url/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy webhook secret/i })).toBeInTheDocument();
  });
});
