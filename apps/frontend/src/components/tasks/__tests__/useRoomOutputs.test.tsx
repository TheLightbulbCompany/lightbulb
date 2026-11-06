import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TaskSummary } from "@lightbulb/shared";
import { fakeTasksClient, Fresh } from "./fakeTasks";

const live = vi.hoisted(() => ({ tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useOptionalLiveEvents: () => live }));

import { roomOutputs, useRoomOutputs } from "../useRoomOutputs";

const task = (taskId: string, output?: string): TaskSummary => ({
  taskId, title: `Request ${taskId}`, status: "resolved", shownGroup: "resolved", watching: false,
  output: output ? { idemKey: `k-${taskId}`, kind: "document", title: output } : null,
} as TaskSummary);

describe("room outputs (Files › Made by requests)", () => {
  it("lists only the requests that made something, with the request beside it", () => {
    expect(roomOutputs([task("t1", "Q3 plan"), task("t2"), task("t3", "Landing page")])).toEqual([
      { taskId: "t1", title: "Q3 plan", request: "Request t1" },
      { taskId: "t3", title: "Landing page", request: "Request t3" },
    ]);
  });

  it("reads the room's list, and Show older adds the next page's outputs", async () => {
    const client = fakeTasksClient([]);
    client.list
      .mockResolvedValueOnce({ tasks: [task("t1", "Q3 plan")], next: "c1" as never })
      .mockResolvedValueOnce({ tasks: [task("t0", "Old deck")], next: null });
    live.tasks = client;
    const { result } = renderHook(() => useRoomOutputs("room-1"), { wrapper: Fresh });
    await waitFor(() => expect(result.current.outputs.map((o) => o.title)).toEqual(["Q3 plan"]));
    expect(client.list).toHaveBeenCalledWith({ kind: "room", roomId: "room-1" });
    expect(result.current.hasOlder).toBe(true);
    await act(async () => { await result.current.loadOlder(); });
    expect(client.list).toHaveBeenLastCalledWith({ kind: "room", roomId: "room-1" }, "c1");
    expect(result.current.outputs.map((o) => o.title)).toEqual(["Q3 plan", "Old deck"]);
    expect(result.current.hasOlder).toBe(false);
  });

  it("no room: no read", () => {
    const client = fakeTasksClient([]);
    live.tasks = client;
    const { result } = renderHook(() => useRoomOutputs(null), { wrapper: Fresh });
    expect(result.current.outputs).toEqual([]);
    expect(client.list).not.toHaveBeenCalled();
  });
});
