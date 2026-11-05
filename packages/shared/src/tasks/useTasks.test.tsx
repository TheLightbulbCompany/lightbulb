// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { SWRConfig, useSWRConfig } from "swr";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import type { TasksClient } from "./client";
import { fixtureTask } from "./fixtures";
import type { TaskDetail, TaskSummary } from "./types";
import { refreshTask, taskKey, taskListKey, useOlderTasks, useTask, useTaskList } from "./useTasks";

const t1 = fixtureTask({ taskId: "t1", title: "one", version: 5 });
const t2 = fixtureTask({ taskId: "t2", title: "two", version: 2 });
const detailOf = (task: TaskSummary): TaskDetail => ({ task, attempts: [], questions: [], effects: [], subscriptions: [], taskRoomId: task.taskRoomId });

function fakeClient(over: Partial<TasksClient> = {}) {
  return {
    cacheScope: "org_a",
    list: vi.fn(async () => ({ tasks: [t1, t2], next: null })),
    get: vi.fn(async (id: string) => detailOf(id === "t1" ? t1 : t2)),
    ...over,
  } as unknown as TasksClient & { list: ReturnType<typeof vi.fn>; get: ReturnType<typeof vi.fn> };
}

async function setup(client: TasksClient) {
  const wrapper = ({ children }: { children: ReactNode }) =>
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
  const hook = renderHook(() => ({
    list: useTaskList(client, { kind: "room", roomId: "r1" }),
    d1: useTask(client, "t1"),
    d2: useTask(client, "t2"),
    swr: useSWRConfig(),
  }), { wrapper });
  await waitFor(() => expect(hook.result.current.list.data && hook.result.current.d1.data && hook.result.current.d2.data).toBeTruthy());
  const deps = () => ({ cache: hook.result.current.swr.cache, mutate: hook.result.current.swr.mutate, client });
  return { hook, deps };
}

describe("useTasks", () => {
  it("list keys carry the workspace, so a workspace switch never reuses another tenant's list", () => {
    expect(taskListKey({ cacheScope: "org_a" }, { kind: "mine" })).toBe("tasks:org_a:mine");
    expect(taskListKey({ cacheScope: "org_b" }, { kind: "mine" })).not.toBe(taskListKey({ cacheScope: "org_a" }, { kind: "mine" }));
  });

  it("null key when off", () => {
    const client = fakeClient();
    renderHook(() => [useTaskList(client, null), useTask(client, null)]);
    expect(client.list).not.toHaveBeenCalled();
    expect(client.get).not.toHaveBeenCalled();
  });

  it("the list reads one page; older pages load only on demand", async () => {
    const client = fakeClient({
      list: vi.fn(async (_scope: unknown, cursor?: string | null) => (cursor ? { tasks: [t2], next: null } : { tasks: [t1], next: "c1" })),
    } as never);
    const { hook } = await setup(client);
    expect(hook.result.current.list.data?.tasks.map((t) => t.taskId)).toEqual(["t1"]);
    expect(client.list).toHaveBeenCalledTimes(1);
    const wrapper = ({ children }: { children: ReactNode }) => <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;
    const scope = { kind: "room", roomId: "r1" } as const;
    const older = renderHook(() => useOlderTasks(client, hook.result.current.list.data, scope), { wrapper });
    expect(older.result.current.hasOlder).toBe(true);
    await act(async () => { await older.result.current.loadOlder(); });
    expect(client.list).toHaveBeenLastCalledWith(scope, "c1");
    expect(older.result.current.older.map((t) => t.taskId)).toEqual(["t2"]);
    expect(older.result.current.hasOlder).toBe(false);
  });

  it("task.changed refetches only the matching task", async () => {
    const t1v6 = { ...t1, version: 6, title: "one (new)" };
    const client = fakeClient();
    const { hook, deps } = await setup(client);
    client.get.mockClear();
    client.list.mockClear();
    client.get.mockImplementationOnce(async () => detailOf(t1v6));
    await act(() => refreshTask(deps(), { taskId: "t1", part: "task", version: 6 }));
    expect(client.get.mock.calls).toEqual([["t1"]]);
    expect(client.list).not.toHaveBeenCalled();
    expect(hook.result.current.list.data!.tasks[0].title).toBe("one (new)");
    expect(hook.result.current.d1.data!.task.version).toBe(6);
    expect(hook.result.current.d2.data!.task).toBe(t2);
  });

  it("older version is ignored", async () => {
    const client = fakeClient();
    const { deps } = await setup(client);
    client.get.mockClear();
    await act(() => refreshTask(deps(), { taskId: "t1", part: "task", version: 5 }));
    expect(client.get).not.toHaveBeenCalled();
  });

  it("an unknown task revalidates the lists", async () => {
    const client = fakeClient();
    const { deps } = await setup(client);
    client.list.mockClear();
    client.get.mockClear();
    await act(() => refreshTask(deps(), { taskId: "t-new", part: "task", version: 1 }));
    expect(client.list).toHaveBeenCalledTimes(1);
    expect(client.get).not.toHaveBeenCalled();
  });

  it("forbidden refetch drops the task quietly", async () => {
    const client = fakeClient();
    const { hook, deps } = await setup(client);
    client.get.mockClear();
    client.get.mockImplementationOnce(async () => { throw new ApiError(403, null); });
    await act(() => refreshTask(deps(), { taskId: "t1", part: "task", version: 9 }));
    expect(client.get).toHaveBeenCalledTimes(1);
    expect(hook.result.current.list.data!.tasks.map((t) => t.taskId)).toEqual(["t2"]);
    expect(hook.result.current.swr.cache.get(taskKey("t1"))?.data).toBeUndefined();
    expect(hook.result.current.list.error).toBeUndefined();
  });
});
