// Home's grouping, and the one count three places show: Home's Needs you heading, the Home rail badge
// (useNeedsYouCount) and each room's About dot (useRoomThreads) all cut the same summaries the same way.
import { render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { overviewGroups, type TaskSummary } from "@lightbulb/shared";
import { SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ tasks: null as unknown, agentNames: new Map() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => ({ items: [] }) }));
// The server's complete Needs you read (`/tasks?needsYou=true`) answers from the same rows, by scope; automations too.
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));
const openPanel = vi.hoisted(() => vi.fn());
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({ directoryProps: { rooms: [{ roomId: "r1", kind: "channel", name: "cafe menu", participants: [] }] } }),
}));
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel }));

import { HomeWork } from "../HomeWork";
import { useNeedsYouCount, useRoomThreads } from "../useRoomThreads";

const task = (taskId: string, roomId: string, shownGroup: TaskSummary["shownGroup"], extra: Partial<TaskSummary> = {}): TaskSummary => ({
  taskId, identifier: taskId, title: `Request ${taskId}`, status: shownGroup === "resolved" ? "resolved" : shownGroup === "idle" ? "idle" : "working",
  shownGroup, audience: { kind: "room", roomId }, audienceKind: "room", originRoomId: roomId, taskRoomId: `tr-${taskId}`,
  watching: false, updatedAt: `2026-11-0${(taskId.charCodeAt(taskId.length - 1) % 9) + 1}T00:00:00Z`, version: 1, ...extra,
});

// Two rooms; Needs you = waiting on you + ready for review; a done request never counts.
const TASKS: TaskSummary[] = [
  task("a", "r1", "waiting_on_you"), task("b", "r1", "ready_for_review"), task("c", "r1", "working"),
  task("d", "r2", "waiting_on_you"), task("e", "r2", "landing"),
  task("g", "r2", "idle", { watching: true, watchUntil: "2026-12-01T00:00:00Z" }),
  ...["h", "i", "j", "k", "l", "m"].map((id) => task(id, id < "k" ? "r1" : "r2", "resolved")),
  ...["n", "o", "p", "q", "r", "s"].map((id) => task(id, id < "q" ? "r1" : "r2", "idle")),
  // Someone else's failure: the server files it under idle; it reads Closed, not Done.
  task("t", "r1", "idle", { status: "failed" }),
];
const roomOf = (roomId: string) => TASKS.filter((t) => t.originRoomId === roomId);

let client: FakeTasksClient;
beforeEach(() => {
  client = fakeTasksClient(TASKS); live.tasks = client; openPanel.mockClear();
  apiGet.mockImplementation(async (path: string) => {
    if (path === "/automations") return { automations: [
      { id: "a1", name: "Weekly signups", status: "active", deliverTo: "room:r1", schedule: { cron: ["0 9 * * 1"], tz: "America/New_York" } },
      { id: "a2", name: "Morning digest", status: "paused", deliverTo: "dm", schedule: null },
      // A builtin job is Settings' (Proactive), never counted here.
      { id: "builtin:feed", name: "Feed posts", status: "active", deliverTo: "none", schedule: null },
      // A one-off already past won't run again: not Scheduled (W13).
      { id: "a3", name: "Post the signups once", status: "paused", pausedReason: "ran once", deliverTo: "room:r1", schedule: { cron: ["21 6 5 11 *"], tz: "UTC", remainingRuns: 1 } },
    ] };
    const scope = decodeURIComponent(/scope=([^&]+)/.exec(path)![1]);
    const rows = scope.startsWith("room:") ? roomOf(scope.slice(5)) : TASKS;
    return { tasks: rows.filter((t) => t.shownGroup === "waiting_on_you" || t.shownGroup === "ready_for_review"), next: null };
  });
});

describe("Home's groups are the shared four (overviewGroups)", () => {
  it("Home's counts are the sum of the per-room counts, group by group", () => {
    const count = (ts: TaskSummary[]) => Object.fromEntries(overviewGroups(ts).map((g) => [g.id, g.tasks.map((t) => t.taskId)]));
    const all = count(TASKS);
    const rooms = ["r1", "r2"].map((r) => count(roomOf(r)));
    for (const id of ["needs_you", "working", "done", "closed"]) {
      expect(all[id].length).toBe(rooms.reduce((n, r) => n + (r[id]?.length ?? 0), 0));
    }
    expect(all.needs_you).toEqual(["a", "b", "d"]);
    expect(all.working).toEqual(["c", "e", "g"]);
    expect(all.done).toHaveLength(6);
    expect(all.closed).toHaveLength(7);
  });
});

describe("HomeWork", () => {
  it("renders Needs you, Working, Done (collapsed, folded after five) and Closed (collapsed) with the same count the badges read", async () => {
    const onOpen = vi.fn();
    const older = ["u", "v"].map((id) => task(id, "r1", "idle", { updatedAt: "2026-10-01T00:00:00Z" }));
    client.list.mockImplementation((async (_scope: unknown, cursor?: unknown) => cursor === "page-2"
      ? { tasks: older, next: null } : { tasks: TASKS, next: "page-2" }) as never);
    render(<Fresh><HomeWork viewerId={SAM} onOpen={onOpen} /></Fresh>);
    const needs = await screen.findByRole("region", { name: "Needs you · 3" });
    // A collapsed count is not an empty list (W11-4): no "Show more" until Done or Closed is open.
    await screen.findByRole("region", { name: "Done · 6" });
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    expect(screen.getByRole("region", { name: "Working · 3" })).toBeInTheDocument();
    // Done collapses by default, like Closed (07 §2).
    const done = screen.getByRole("region", { name: "Done · 6" });
    expect(within(done).queryAllByRole("listitem")).toHaveLength(0);
    await userEvent.click(within(done).getByRole("button", { name: "Done · 6" }));
    expect(within(done).getByRole("button", { name: "Done · 6" })).toHaveAttribute("aria-expanded", "true");
    expect(within(done).getAllByRole("listitem")).toHaveLength(5);
    // One "Show more" per group: one click shows the next five, fetching the next page when the loaded rows run short
    // (6 here), so its rows appear without a second click.
    await userEvent.click(within(done).getByRole("button", { name: "Show more" }));
    await waitFor(() => expect(within(done).getAllByRole("listitem")).toHaveLength(8));
    expect(within(done).queryByRole("button", { name: "Show more" })).toBeNull();
    // Activity spans projects: a row names its project.
    expect(within(done).getByText("Request n").closest("li")).toHaveTextContent("Request n· #cafe menu");
    // Closed (resolved and someone else's failure) is its own group, collapsed until opened.
    const closed = screen.getByRole("region", { name: "Closed · 7" });
    expect(within(closed).queryAllByRole("listitem")).toHaveLength(0);
    await userEvent.click(within(closed).getByRole("button", { name: "Closed · 7" }));
    expect(within(closed).getAllByRole("listitem")).toHaveLength(5);
    await userEvent.click(within(closed).getByRole("button", { name: "Show more" }));
    expect(within(closed).getAllByRole("listitem")).toHaveLength(7);
    // A row opens the request in the pane.
    await userEvent.click(within(needs).getByRole("button", { name: /^Open Request a,/ }));
    expect(onOpen).toHaveBeenCalledWith("a");
    // Work dissolved (UX slice 4): Scheduled · N is a group here (collapsed, builtin jobs excluded), and a row opens
    // the automation in its project's pane, by URL; one that posts to a DM opens beside Activity.
    const scheduled = await screen.findByRole("region", { name: "Scheduled · 2" });
    expect(within(scheduled).queryAllByRole("listitem")).toHaveLength(0);
    await userEvent.click(within(scheduled).getByRole("button", { name: "Scheduled · 2" }));
    expect(within(scheduled).queryByText("Feed posts")).toBeNull();
    expect(within(scheduled).getByText("Paused")).toBeInTheDocument();
    await userEvent.click(within(scheduled).getByRole("button", { name: /Weekly signups/ }));
    expect(new URLSearchParams(window.location.search).get("pane.id")).toBe("a1");
    expect(new URLSearchParams(window.location.search).get("room")).toBe("r1");
    await userEvent.click(within(scheduled).getByRole("button", { name: /Morning digest/ }));
    expect(openPanel).toHaveBeenCalledWith("automation", { id: "a2" }, { pushUrl: true });
    expect(within(scheduled).queryByText("Post the signups once")).toBeNull();
    // The old task board is one link away, once, at the foot.
    expect(screen.getAllByRole("button", { name: /old board/ })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /old board/ }));
    expect(openPanel).toHaveBeenCalledWith("teams-issues", {}, { pushUrl: true });

    // The rail badge and each room's About dot count the same rows.
    const { result: badge } = renderHook(() => useNeedsYouCount(), { wrapper: Fresh });
    await waitFor(() => expect(badge.current).toBe(3));
  });

  it("each room's About dot counts that room's Needs you rows, from the complete read", async () => {
    const { result } = renderHook(() => [useRoomThreads("r1"), useRoomThreads("r2")], { wrapper: Fresh });
    await waitFor(() => expect(result.current.map((r) => r.waitingOnMe)).toEqual([2, 1]));
    // One workspace-wide read serves every room.
    expect(apiGet.mock.calls.filter(([p]) => String(p).includes("needsYou")).map(([p]) => p)).toEqual(["/tasks?scope=workspace&needsYou=true"]);
  });

  it("Needs you is complete: a request beyond the list's first page still shows and counts", async () => {
    const old = task("z", "r2", "waiting_on_you", { updatedAt: "2025-01-01T00:00:00Z" });
    client.list.mockResolvedValue({ tasks: TASKS.slice(0, 3), next: "page-2" as never }); // the paged list never reaches it
    const base = apiGet.getMockImplementation()!;
    apiGet.mockImplementation(async (path: string) => path.includes("needsYou=true")
      ? { tasks: [...(await base(path)).tasks, old], next: null } : base(path));
    render(<Fresh><HomeWork viewerId={SAM} onOpen={vi.fn()} /></Fresh>);
    const needs = await screen.findByRole("region", { name: "Needs you · 4" });
    expect(within(needs).getByText("Request z")).toBeInTheDocument();
    // No Done or Closed group on this page: the older pages are one link away.
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
    const { result: badge } = renderHook(() => useNeedsYouCount(), { wrapper: Fresh });
    await waitFor(() => expect(badge.current).toBe(4));
  });

  it("your company today, in order: Needs you, then the slots (mentions, Ideas, Feed), then Working, Done, Closed", async () => {
    render(<Fresh><HomeWork viewerId={SAM} onOpen={vi.fn()} between={<section aria-label="Ideas slot" />} /></Fresh>);
    const needs = await screen.findByRole("region", { name: "Needs you · 3" });
    const slot = screen.getByRole("region", { name: "Ideas slot" });
    const working = await screen.findByRole("region", { name: "Working · 3" });
    const closed = screen.getByRole("region", { name: "Closed · 7" });
    const order = [needs, slot, working, closed];
    for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("Needs you holds the non-task asks too, in one count", async () => {
    render(<Fresh><HomeWork viewerId={SAM} onOpen={vi.fn()} askCount={1} asks={<p>Sign in to porkbun.com again</p>} /></Fresh>);
    const needs = await screen.findByRole("region", { name: "Needs you · 4" });
    expect(within(needs).getByText("Sign in to porkbun.com again")).toBeInTheDocument();
  });

  it("the slots show while requests are still loading", () => {
    client.list.mockReturnValue(new Promise(() => {}));
    render(<Fresh><HomeWork viewerId={SAM} onOpen={vi.fn()} between={<section aria-label="Ideas slot" />} /></Fresh>);
    expect(screen.getByRole("region", { name: "Ideas slot" })).toBeInTheDocument();
  });

  it("a working row shows who is doing it and the clock, which never truncates (dev walk 13)", async () => {
    render(<Fresh><HomeWork viewerId={SAM} onOpen={vi.fn()} /></Fresh>);
    const working = await screen.findByRole("region", { name: "Working · 3" });
    const row = within(working).getByText("Request c").closest("li")!;
    expect(row).toHaveTextContent(/— Lightbulb/);
    expect(row.querySelector("time.tabular-nums")!.parentElement).toHaveClass("shrink-0");
    expect(row.querySelector("time.tabular-nums")).toHaveTextContent(/^\d+:\d\d:\d\d$/);
  });
});
