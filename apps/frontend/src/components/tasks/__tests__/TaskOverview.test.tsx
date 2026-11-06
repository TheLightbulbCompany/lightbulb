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
const apiGet = vi.hoisted(() => vi.fn(async (): Promise<unknown> => ({ automations: [] })));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));
const copyText = vi.hoisted(() => vi.fn(async () => true));
vi.mock("@/components/messaging/messageLinks", async (importOriginal) => ({ ...await importOriginal<typeof import("@/components/messaging/messageLinks")>(), copyText }));

import { TaskOverview } from "../TaskOverview";
import { ScheduledList } from "../ScheduledList";

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
    expect(headings()).toEqual(["Needs you · 0", "Working · 3", "Closed · 0"]);
    expect(screen.getByText("Nothing needs you")).toBeInTheDocument();
    expect(screen.queryByText("Nothing closed yet")).toBeNull(); // collapsed by default
    expect(screen.queryByRole("heading", { name: /Done/ })).toBeNull();
  });

  it("closed collapsed, toggles and remembers", async () => {
    client = fakeTasksClient(FIXTURES.step09.tasks); live.tasks = client;
    overview();
    const toggle = await screen.findByRole("button", { name: "Closed · 1" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Workshop signup app")).toBeNull();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Open Workshop signup app, Closed" })).toBeInTheDocument();
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
    await screen.findByRole("heading", { name: "Needs you · 1" });
    const waiting = screen.getByRole("region", { name: "Needs you · 1" });
    expect(within(waiting).getByText("Workshop signup app")).toBeInTheDocument();
    expect(within(waiting).getByText(/^Needs approval: Provision database and Stripe/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Working · 2" })).toBeInTheDocument();
  });

  it("rows open the request; Reopen acts on a closed row", async () => {
    client = fakeTasksClient(FIXTURES.step09.tasks); live.tasks = client;
    const onOpen = vi.fn();
    overview(room, onOpen);
    // Done is collapsed by default (07 §2); its rows are one line, with no status word of their own.
    await userEvent.click(await screen.findByRole("button", { name: "Done · 1" }));
    const row = screen.getByRole("button", { name: "Open Stripe fee estimate, Done" }).closest("li")!;
    expect(row.textContent!.match(/Done/g) ?? []).toHaveLength(0);
    expect(row.querySelectorAll("p")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Open Stripe fee estimate, Done" }));
    expect(onOpen).toHaveBeenCalledWith("t-fees");
    await userEvent.click(screen.getByRole("button", { name: "Closed · 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Reopen" }));
    expect(client.act).toHaveBeenCalledWith("t-signup", "reopen", 7);
  });

  it("Done opens by itself when it is the only group with tasks; a press still folds it (dev walk 13, W13-11)", async () => {
    const fees = FIXTURES.step09.tasks.find((t) => t.taskId === "t-fees")!;
    client = fakeTasksClient([fees]); live.tasks = client;
    overview();
    const toggle = await screen.findByRole("button", { name: "Done · 1" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Open Stripe fee estimate, Done" })).toBeInTheDocument();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("a row's ⋯ says Close, not Mark done (dev walk 13, W13-3)", async () => {
    client = fakeTasksClient(FIXTURES.step09.tasks); live.tasks = client;
    overview();
    await userEvent.click(await screen.findByRole("button", { name: "Done · 1" }));
    await userEvent.click(screen.getByRole("button", { name: "More for Stripe fee estimate" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Close" }));
    expect(client.act).toHaveBeenCalledWith("t-fees", "resolve", expect.any(Number));
  });

  it("Copy link copies the thread's permalink and says so", async () => {
    overview();
    await userEvent.click(await screen.findByRole("button", { name: "More for Workshop signup app" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Copy link" }));
    expect(copyText).toHaveBeenCalledWith(expect.stringMatching(/\/chat\?panel=task-detail&taskId=t-signup$/));
    expect(await screen.findByRole("status")).toHaveTextContent("Link copied");
  });

  it("no Scheduled tab: scheduled work is its own view (UX slice 4)", async () => {
    overview();
    await screen.findByText(/Needs you/);
    expect(screen.queryByRole("tab", { name: "Scheduled" })).toBeNull();
  });

  it("the Scheduled view shows the empty state until automations exist", async () => {
    render(<Fresh><ScheduledList scope={room} onOpen={vi.fn()} /></Fresh>);
    expect(await screen.findByText("Ask @lightbulb to put recurring work on a schedule.")).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith("/automations");
    expect(client.cacheScope).toBe("org_test"); // the SWR key carries it: ["automations", cacheScope]
  });

  it("a project's empty Scheduled view: three starter cards; a tap only drafts the ask (UX slice 6)", async () => {
    const onDraft = vi.fn();
    apiGet.mockResolvedValueOnce({ automations: [{ id: "builtin:look-around", name: "Daily look around", deliverTo: null }] });
    render(<Fresh><ScheduledList scope={room} onOpen={vi.fn()} onDraft={onDraft} /></Fresh>);
    const cards = await screen.findByRole("list", { name: "Starter schedules" });
    expect(screen.queryByText("Daily look around")).toBeNull(); // a builtin job is never a project's schedule
    expect(within(cards).getAllByRole("button")).toHaveLength(3);
    await userEvent.click(within(cards).getByRole("button", { name: /Weekly signups/ }));
    expect(onDraft).toHaveBeenCalledWith("Every Monday at 9:00 AM, have Growth post last week's signups here.");
    expect(apiGet).toHaveBeenCalledTimes(1); // read once; nothing created
  });

  it("the Scheduled view: a failed read says so with Retry, never the empty prompt", async () => {
    apiGet.mockRejectedValueOnce(new Error("503"));
    render(<Fresh><ScheduledList scope={room} onOpen={vi.fn()} /></Fresh>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Schedules couldn't load.");
    expect(screen.queryByText("Ask @lightbulb to put recurring work on a schedule.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Ask @lightbulb to put recurring work on a schedule.")).toBeInTheDocument();
  });
});
