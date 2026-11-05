// Home's grouping, and the one count three places show: Home's Needs you heading, the Home rail badge
// (useHomeNeedsYou) and each room's About dot (useRoomThreads) all cut the same summaries the same way.
import { render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { overviewGroups, type TaskSummary } from "@lightbulb/shared";
import { SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient, type FakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, agentNames: new Map() }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
// The server's complete Needs you read (`/tasks?needsYou=true`) answers from the same rows, by scope; automations too.
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));
const openPanel = vi.hoisted(() => vi.fn());
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel }));

import { HomeWork } from "../HomeWork";
import { useHomeNeedsYou, useRoomThreads } from "../useRoomThreads";

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
    if (path === "/automations") return { automations: [{ id: "a1" }, { id: "a2" }] };
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
  it("renders Needs you, Working, Done (folded after five) and Closed (collapsed) with the same count the badges read", async () => {
    const onOpen = vi.fn();
    render(<Fresh><HomeWork viewerId={SAM} onOpen={onOpen} /></Fresh>);
    const needs = await screen.findByRole("region", { name: "Needs you · 3" });
    expect(screen.getByRole("region", { name: "Working · 3" })).toBeInTheDocument();
    const done = screen.getByRole("region", { name: "Done · 6" });
    expect(within(done).getAllByRole("listitem")).toHaveLength(5);
    await userEvent.click(within(done).getByRole("button", { name: "Show all 6" }));
    expect(within(done).getAllByRole("listitem")).toHaveLength(6);
    // Closed (resolved and someone else's failure) is its own group, collapsed until opened.
    const closed = screen.getByRole("region", { name: "Closed · 7" });
    expect(within(closed).queryAllByRole("listitem")).toHaveLength(0);
    await userEvent.click(within(closed).getByRole("button", { name: "Closed · 7" }));
    expect(within(closed).getAllByRole("listitem")).toHaveLength(7);
    // A row opens the request in the pane.
    await userEvent.click(within(needs).getByRole("button", { name: /^Open Request a,/ }));
    expect(onOpen).toHaveBeenCalledWith("a");
    // The scheduled line links to the full Automations list.
    expect(await screen.findByText(/2 scheduled automations/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Automations" }));
    expect(openPanel).toHaveBeenCalledWith("teams-routines", {}, { pushUrl: true });

    // The rail badge and each room's About dot count the same rows.
    const { result: badge } = renderHook(() => useHomeNeedsYou(), { wrapper: Fresh });
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
    const { result: badge } = renderHook(() => useHomeNeedsYou(), { wrapper: Fresh });
    await waitFor(() => expect(badge.current).toBe(4));
  });
});
