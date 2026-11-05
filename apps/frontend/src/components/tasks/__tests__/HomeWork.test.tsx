// Home's grouping, and the one count three places show: Home's Needs you heading, the Home rail badge
// (useHomeNeedsYou) and each room's About dot (useRoomThreads) all cut the same summaries the same way.
import { render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskSummary } from "@lightbulb/shared";
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
import { homeSections } from "../homeSections";
import { useHomeNeedsYou, useRoomThreads } from "../useRoomThreads";

const task = (taskId: string, roomId: string, shownGroup: TaskSummary["shownGroup"], extra: Partial<TaskSummary> = {}): TaskSummary => ({
  taskId, identifier: taskId, title: `Request ${taskId}`, status: shownGroup === "resolved" ? "resolved" : shownGroup === "idle" ? "idle" : "working",
  shownGroup, audience: { kind: "room", roomId }, audienceKind: "room", originRoomId: roomId, taskRoomId: `tr-${taskId}`,
  watching: false, updatedAt: `2026-11-0${(taskId.charCodeAt(taskId.length - 1) % 9) + 1}T00:00:00Z`, version: 1, ...extra,
});

// Two rooms; Needs you = waiting on you + ready for review; a proposal and a done request never count.
const TASKS: TaskSummary[] = [
  task("a", "r1", "waiting_on_you"), task("b", "r1", "ready_for_review"), task("c", "r1", "working"),
  task("d", "r2", "waiting_on_you"), task("e", "r2", "landing"), task("f", "r2", "proposed" as never, { status: "proposed", shownGroup: null }),
  task("g", "r2", "idle", { watching: true, watchUntil: "2026-12-01T00:00:00Z" }),
  ...["h", "i", "j", "k", "l", "m"].map((id) => task(id, id < "k" ? "r1" : "r2", "resolved")),
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

describe("homeSections", () => {
  it("Home's counts are the sum of the per-room counts, group by group", () => {
    const all = homeSections(TASKS);
    const rooms = ["r1", "r2"].map((r) => homeSections(roomOf(r)));
    for (const key of ["needsYou", "working", "done"] as const) {
      expect(all[key].length).toBe(rooms.reduce((n, r) => n + r[key].length, 0));
    }
    expect(all.needsYou.map((t) => t.taskId)).toEqual(["a", "b", "d"]);
    expect(all.working.map((t) => t.taskId)).toEqual(["c", "e", "g"]);
    expect(all.done).toHaveLength(6);
  });
});

describe("HomeWork", () => {
  it("renders Needs you, Working and Done (folded after five) with the same count the badges read", async () => {
    const onOpen = vi.fn();
    render(<Fresh><HomeWork viewerId={SAM} onOpen={onOpen} /></Fresh>);
    const needs = await screen.findByRole("region", { name: "Needs you · 3" });
    expect(screen.getByRole("region", { name: "Working · 3" })).toBeInTheDocument();
    const done = screen.getByRole("region", { name: "Done · 6" });
    expect(within(done).getAllByRole("listitem")).toHaveLength(5);
    await userEvent.click(within(done).getByRole("button", { name: "Show all 6" }));
    expect(within(done).getAllByRole("listitem")).toHaveLength(6);
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
    const { result: r1 } = renderHook(() => useRoomThreads("r1"), { wrapper: Fresh });
    const { result: r2 } = renderHook(() => useRoomThreads("r2"), { wrapper: Fresh });
    await waitFor(() => expect(r1.current.waitingOnMe).toBe(2));
    await waitFor(() => expect(r2.current.waitingOnMe).toBe(1));
    expect(apiGet).toHaveBeenCalledWith("/tasks?scope=room%3Ar1&needsYou=true");
  });

  it("Needs you is complete: a request beyond the list's first page still shows and counts", async () => {
    const old = task("z", "r2", "waiting_on_you", { updatedAt: "2025-01-01T00:00:00Z" });
    client.list.mockResolvedValue({ tasks: TASKS.slice(0, 3), next: "page-2" }); // the paged list never reaches it
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
