import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AutomationView } from "@lightbulb/shared";
import { FIXTURES } from "@lightbulb/shared/tasks/fixtures";
import { Fresh } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: { cacheScope: "org_test" } }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(async () => ({})), del: vi.fn(async () => undefined) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("@/components/teams/panels/RoutinesPanel", () => ({ RoutinesPanel: () => <div data-testid="routines-panel">Routines (today)</div> }));
const openTaskPane = vi.hoisted(() => vi.fn());
vi.mock("@/components/workspace/workspaceController", () => ({ openTaskPane }));

import { AutomationsPanel } from "../AutomationsPanel";

const signups: AutomationView = {
  id: "a1", name: "Post signups every Monday", instructions: "Post the signup count so far in #workshop-launch.", status: "active", pausedReason: null,
  schedule: { cron: ["0 9 * * 1"], tz: "America/New_York" }, nextRuns: [], specialistId: "finance", deliverTo: "room-workshop-launch",
  budgetPerRunUsd: 1, estRunsPerMonth: 4, ownerUserId: "user_sam", lastRun: { firedAt: "2026-11-02T14:00:00Z", source: "schedule", status: "done", taskId: "t-fees" }, version: 3,
};
const routes = (over: Partial<AutomationView> = {}) => {
  const a = { ...signups, ...over };
  api.get.mockImplementation(async (path: string) => {
    if (path === "/automations") return { automations: [a] };
    if (path === "/automations/a1") return a;
    if (path === "/automations/a1/runs") return { runs: [FIXTURES.step09.tasks[2]], next: null };
    throw new Error(`no route ${path}`);
  });
};
beforeEach(() => { live.superAgent = true; api.get.mockReset(); api.post.mockClear(); openTaskPane.mockClear(); routes(); });

describe("AutomationsPanel", () => {
  it("schedule in the viewer's time zone", async () => {
    const { unmount } = render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    expect(await screen.findByText(/Mondays 9:00/)).toBeInTheDocument();
    unmount();
    render(<Fresh><AutomationsPanel viewerTz="Europe/London" /></Fresh>);
    expect(await screen.findByText(/Mondays 14:00 \(your time\)/)).toBeInTheDocument();
  });

  it("Run now and Pause call lane A's routes", async () => {
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Run now" }));
    expect(api.post).toHaveBeenCalledWith("/automations/a1/run", {});
    await userEvent.click(screen.getByRole("button", { name: "Post signups every Monday" }));
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(api.post).toHaveBeenCalledWith("/automations/a1/pause", {});
    expect(within(screen.getByRole("region", { name: "Instructions" })).getByText(/signup count/)).toBeInTheDocument();
  });

  it("history rows open their threads", async () => {
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Post signups every Monday" }));
    const history = await screen.findByRole("region", { name: "History" });
    await userEvent.click(await within(history).findByRole("button", { name: /Stripe fee estimate/ }));
    expect(openTaskPane).toHaveBeenCalledWith("t-fees");
  });

  it("off: RoutinesPanel unchanged and no automations read; on: Older automations reaches it", async () => {
    live.superAgent = false;
    const { unmount } = render(<Fresh><AutomationsPanel /></Fresh>);
    expect(screen.getByTestId("routines-panel")).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
    unmount();
    live.superAgent = true;
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Older automations" }));
    expect(screen.getByTestId("routines-panel")).toBeInTheDocument();
  });
});
