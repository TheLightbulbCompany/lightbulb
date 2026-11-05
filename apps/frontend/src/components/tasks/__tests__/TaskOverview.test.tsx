import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { useSWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { taskListKey, type TaskScope } from "@lightbulb/shared";
import { FIXTURES, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const apiGet = vi.hoisted(() => vi.fn(async () => { throw new Error("no automations route"); }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));

import { TaskOverview } from "../TaskOverview";

let client: FakeTasksClient;
let swr: ReturnType<typeof useSWRConfig>;
function Probe() { const config = useSWRConfig(); useEffect(() => { swr = config; }, [config]); return null; }
const room: TaskScope = { kind: "room", roomId: "r1" };
const overview = (scope: TaskScope = room, onOpen = vi.fn()) =>
  render(<Fresh><Probe /><TaskOverview scope={scope} viewerId={SAM} onOpen={onOpen} /></Fresh>);
const headings = () => screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);

beforeEach(() => {
  client = fakeTasksClient(FIXTURES.step03.tasks);
  live.tasks = client;
  try { localStorage.clear(); } catch { /* jsdom */ }
});

describe("TaskOverview", () => {
  it("group order and placeholders", async () => {
    overview();
    await screen.findByRole("heading", { name: "Working · 3" });
    expect(headings()).toEqual(["Waiting on you · 0", "Working · 3", "Resolved · 0"]);
    expect(screen.getByText("Nothing needs you")).toBeInTheDocument();
    expect(screen.queryByText("Nothing resolved yet")).toBeNull(); // collapsed by default
    expect(screen.queryByRole("heading", { name: /Ready for review|Landing|Idle/ })).toBeNull();
  });

  it("resolved collapsed, toggles and remembers", async () => {
    client = fakeTasksClient(FIXTURES.step09.tasks); live.tasks = client;
    overview();
    const toggle = await screen.findByRole("button", { name: "Resolved · 1" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Workshop signup app")).toBeNull();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Open Workshop signup app, Resolved" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reopen" })).toBeInTheDocument();
  });

  it("scopes call the right route", async () => {
    overview({ kind: "mine" });
    await screen.findByRole("heading", { name: /Working/ });
    expect(client.list).toHaveBeenCalledWith({ kind: "mine" });
    overview({ kind: "workspace" });
    await act(async () => {});
    expect(client.list).toHaveBeenCalledWith({ kind: "workspace" });
  });

  it("live move between groups", async () => {
    overview();
    await screen.findByRole("heading", { name: "Working · 3" });
    const moved = FIXTURES.step05.tasks; // signup now waits on Sam
    client.list.mockResolvedValue({ tasks: moved, next: null });
    await act(() => swr.mutate(taskListKey(client, room)));
    await screen.findByRole("heading", { name: "Waiting on you · 1" });
    const waiting = screen.getByRole("region", { name: "Waiting on you · 1" });
    expect(within(waiting).getByText("Workshop signup app")).toBeInTheDocument();
    expect(within(waiting).getByText("Needs approval: Provision database and Stripe")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Working · 2" })).toBeInTheDocument();
  });

  it("rows open the thread; Reopen acts on a resolved row", async () => {
    client = fakeTasksClient(FIXTURES.step09.tasks); live.tasks = client;
    const onOpen = vi.fn();
    overview(room, onOpen);
    await userEvent.click(await screen.findByRole("button", { name: "Open Stripe fee estimate, Idle" }));
    expect(onOpen).toHaveBeenCalledWith("t-fees");
    await userEvent.click(screen.getByRole("button", { name: "Resolved · 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Reopen" }));
    expect(client.act).toHaveBeenCalledWith("t-signup", "reopen", 7);
  });

  it("scheduled tab shows the empty state until automations exist", async () => {
    overview();
    await userEvent.click(await screen.findByRole("tab", { name: "Scheduled" }));
    expect(await screen.findByText("Ask @lightbulb to put recurring work on a schedule.")).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith("/automations");
  });
});
