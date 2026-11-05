import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AutomationView } from "@lightbulb/shared";
import { FIXTURES } from "@lightbulb/shared/tasks/fixtures";
import { Fresh } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: { cacheScope: "org_test" } }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(async () => ({})), del: vi.fn(async () => undefined), patch: vi.fn(async () => ({})) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("@/components/teams/panels/RoutinesPanel", () => ({ RoutinesPanel: () => <div data-testid="routines-panel">Routines (today)</div> }));
const openTaskPane = vi.hoisted(() => vi.fn());
const openPanel = vi.hoisted(() => vi.fn());
vi.mock("@/components/workspace/workspaceController", () => ({ openTaskPane, openPanel }));
const clerk = vi.hoisted(() => ({ userId: "user_sam", role: "org:admin" as string | undefined }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: clerk.userId }), useOrganization: () => ({ membership: clerk.role ? { role: clerk.role } : null }) }));
import { ApiError } from "@lightbulb/shared";

import { AutomationsPanel } from "../AutomationsPanel";

const signups: AutomationView = {
  id: "a1", name: "Post signups every Monday", instructions: "Post the signup count so far in #workshop-launch.", status: "active", pausedReason: null,
  schedule: { cron: ["0 9 * * 1"], tz: "America/New_York" }, nextRuns: [], hooks: [], specialistId: "finance", deliverTo: "room:room-workshop-launch",
  budgetPerRunUsd: 1, ownerUserId: "user_sam", createdBy: "router", mode: null, lastTaskId: "t-fees", activeRunTaskId: null, runCount: 1, version: 3,
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
beforeEach(() => { live.superAgent = true; clerk.userId = "user_sam"; clerk.role = "org:admin"; api.get.mockReset(); api.post.mockClear(); openTaskPane.mockClear(); routes(); });

describe("AutomationsPanel", () => {
  it("edit moves the time (days kept) and the instructions with the row's version; delete asks first", async () => {
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Post signups every Monday" }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit automation" });
    const box = within(form).getByRole("textbox");
    await userEvent.clear(box);
    await userEvent.type(box, "Post signups and the waitlist.");
    const time = form.querySelector("input[type=time]") as HTMLInputElement;
    await userEvent.clear(time);
    await userEvent.type(time, "10:30");
    await userEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(api.patch).toHaveBeenCalledWith("/automations/a1",
      { instructions: "Post signups and the waitlist.", schedule: { cron: ["30 10 * * 1"], tz: "America/New_York" }, version: 3 });

    await userEvent.click(await screen.findByRole("button", { name: "Delete…" }));
    expect(api.del).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole("group", { name: "Delete Post signups every Monday?" })).getByRole("button", { name: "Delete" }));
    expect(api.del).toHaveBeenCalledWith("/automations/a1");
  });

  it("schedule in the viewer's time zone", async () => {
    const { unmount } = render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    expect(await screen.findByText(/Mondays 9:00/)).toBeInTheDocument();
    unmount();
    render(<Fresh><AutomationsPanel viewerTz="Europe/London" /></Fresh>);
    expect(await screen.findByText(/Mondays 14:00 \(New York time: Mondays 9:00\) · posts in a room/)).toBeInTheDocument();
  });

  it("Run now follows the server's rules: a live run is opened, not restarted; only an admin may run a room automation", async () => {
    routes({ activeRunTaskId: "t-live" });
    const { unmount } = render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Running ›" }));
    expect(openTaskPane).toHaveBeenCalledWith("t-live");
    expect(api.post).not.toHaveBeenCalled();
    unmount();

    routes();
    api.post.mockRejectedValueOnce(new ApiError(409, { detail: { code: "run_in_progress", taskId: "t-other" } }));
    const again = render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Run now" }));
    expect(openTaskPane).toHaveBeenCalledWith("t-other");
    again.unmount();

    // A member who is neither admin nor owner: no Run now, no Pause, the waiting line.
    clerk.role = "org:member";
    clerk.userId = "user_priya";
    const member = render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    expect(await screen.findByRole("button", { name: "Run now" })).toBeDisabled();
    expect(screen.getByText("Only its owner or an admin can run it")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Post signups every Monday" }));
    expect(await screen.findByText("Waiting on a workspace admin")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    member.unmount();

    // The owner (not an admin) may pause and run a room automation (the server's _may_edit), but not resume it;
    // paused, Run now is off (the server's run_now answers 409 not_active).
    clerk.userId = "user_sam";
    routes({ status: "paused", pausedReason: null });
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Post signups every Monday" }));
    expect(await screen.findByRole("button", { name: "Run now" })).toBeDisabled();
    expect(screen.getAllByText("Resume it first").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByText("Waiting on a workspace admin")).toBeInTheDocument();
  });

  it("a rule has no Run now", async () => {
    routes({ match: { sources: ["mail"] } });
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await screen.findByRole("button", { name: "Post signups every Monday" });
    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
  });

  it("Run now and Pause call lane A's routes", async () => {
    render(<Fresh><AutomationsPanel viewerTz="America/New_York" /></Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: "Run now" }));
    expect(api.post).toHaveBeenCalledWith("/automations/a1/run", {});
    await userEvent.click(screen.getByRole("button", { name: "Post signups every Monday" }));
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(api.post).toHaveBeenCalledWith("/automations/a1/pause", {});
    expect(screen.getByText(/up to \$1 a run, about \$4 a month/)).toBeInTheDocument();
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
