// Founder-reported Automations defects (issue #1877): the event list trapped
// the New-automation form, Escape threw the whole dialog away, Run now looked
// like a dead button, and the schedule was a raw cron expression.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SWRResponse } from "swr";

type ReadStub = Partial<SWRResponse<unknown, Error>>;
let reads: Record<string, ReadStub> = {};
const post = vi.fn();
const patch = vi.fn();
const del = vi.fn();
const mutate = vi.fn();

const mockOpenMarketplace = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: mockOpenMarketplace }),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined, mutate },
    post,
    patch,
    del,
  }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue([]), post: vi.fn(), put: vi.fn(), del: vi.fn() }),
}));

const notifySuccess = vi.fn();
const notifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifySuccess: (...args: unknown[]) => notifySuccess(...args),
  notifyApiError: (...args: unknown[]) => notifyApiError(...args),
  notifyConversionGate: () => null,
}));

let connections: Array<Record<string, unknown>> = [];
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections }),
}));

import { RoutinesPanel } from "../RoutinesPanel";

const GITHUB = {
  provider: "github",
  display_name: "GitHub",
  status: "connected",
  inbound: {
    webhook_registration: "auto_github",
    events: [
      { id: "issues.opened", label: "Issue opened" },
      { id: "pull_request.opened", label: "Pull request opened" },
      { id: "push", label: "Push" },
    ],
  },
};

function seedReads(routines: unknown[] = []) {
  reads = {
    "/routines": { data: { routines }, isLoading: false, error: undefined, mutate },
    "/agents": { data: { agents: [] }, isLoading: false, error: undefined, mutate },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  seedReads();
  connections = [];
  post.mockResolvedValue({});
});

describe("RoutinesPanel — New automation dialog", () => {
  async function openConnectedAppRung() {
    connections = [GITHUB];
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: /a connected app/i }));
    // Radix Select: open, then pick GitHub.
    await user.click(within(dialog).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "GitHub" }));
    return { user, dialog };
  }

  it("closes the event list once an event is picked", async () => {
    const { user, dialog } = await openConnectedAppRung();
    await user.click(within(dialog).getByRole("button", { name: /select (an )?events?…/i }));
    await user.click(await screen.findByRole("option", { name: "Push" }));
    // The list is gone and the trigger names the selection — the fields below
    // it (repository, Create) are reachable again.
    await waitFor(() =>
      expect(screen.queryByRole("listbox", { name: "Event" })).not.toBeInTheDocument(),
    );
    expect(within(dialog).getByRole("button", { name: /push/i })).toBeInTheDocument();
  });

  it("Escape closes the event list, not the whole dialog", async () => {
    const { user, dialog } = await openConnectedAppRung();
    await user.click(within(dialog).getByRole("button", { name: /select (an )?events?…/i }));
    expect(await screen.findByRole("listbox", { name: "Event" })).toBeInTheDocument();

    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("listbox", { name: "Event" })).not.toBeInTheDocument(),
    );
    // The dialog — and the app/rung selections made in it — survive.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: /a connected app/i })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("asks for a schedule in words, and stores the cron it builds", async () => {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    // The schedule rung is the default; no raw cron in sight.
    expect(within(dialog).getByText("Every day at 09:00")).toBeInTheDocument();
    expect(within(dialog).queryByDisplayValue("0 9 * * *")).not.toBeInTheDocument();

    await user.type(within(dialog).getByLabelText(/title/i), "Daily digest");
    // `<input type="time">` takes a whole HH:MM value, not keystrokes.
    fireEvent.change(within(dialog).getByLabelText(/time of day/i), {
      target: { value: "07:30" },
    });
    expect(within(dialog).getByText("Every day at 07:30")).toBeInTheDocument();

    post.mockResolvedValueOnce({ id: "r-new" });
    await user.click(within(dialog).getByRole("button", { name: /^create$/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/routines/r-new/triggers",
        expect.objectContaining({ kind: "schedule", cron_expression: "30 7 * * *" }),
      ),
    );
  });

  it("keeps a cron the picker can't express on the raw field", async () => {
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /new automation/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /use a cron expression/i }));
    const raw = within(dialog).getByLabelText(/cron expression/i);
    await user.clear(raw);
    await user.type(raw, "*/13 3 5 6 2");
    // Unrepresentable — the picker must not silently normalize it away.
    expect(raw).toHaveValue("*/13 3 5 6 2");
    expect(within(dialog).queryByLabelText(/time of day/i)).not.toBeInTheDocument();
  });
});

describe("RoutinesPanel — Run now", () => {
  const ROUTINE = { id: "r1", title: "Nightly digest", status: "active", triggers: [] };

  it("acknowledges a started run", async () => {
    seedReads([ROUTINE]);
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /run nightly digest now/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/routines/r1/run", { source: "manual" }),
    );
    await waitFor(() => expect(notifySuccess).toHaveBeenCalled());
    expect(String(notifySuccess.mock.calls[0][0])).toContain("Nightly digest");
  });

  it("surfaces a failed run instead of swallowing it", async () => {
    seedReads([ROUTINE]);
    post.mockRejectedValueOnce(new Error("boom"));
    const user = userEvent.setup();
    render(<RoutinesPanel />);
    await user.click(screen.getByRole("button", { name: /run nightly digest now/i }));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(notifySuccess).not.toHaveBeenCalled();
  });
});
