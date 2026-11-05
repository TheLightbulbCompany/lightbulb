// Test harness for task UI: a fake `tasks` client on a fake live context, and a fresh SWR cache.
import type { ReactNode } from "react";
import { SWRConfig } from "swr";
import { vi } from "vitest";
import type { TaskDetail, TaskSummary, TasksClient } from "@lightbulb/shared";

export function fakeTasksClient(tasks: TaskSummary[], details: Record<string, TaskDetail> = {}) {
  return {
    cacheScope: "org_test",
    list: vi.fn(async () => ({ tasks, next: null })),
    get: vi.fn(async (id: string) => details[id]),
    act: vi.fn(async (id: string, verb: string) => ({ ...tasks.find((t) => t.taskId === id)!, status: verb === "start" ? "working" : "resolved" })),
    startMany: vi.fn(async () => ({})),
    answer: vi.fn(async () => ({ question: {}, next: null })),
    trace: vi.fn(async () => ({})),
    markRead: vi.fn(async () => ({})),
    needsYou: vi.fn(async () => ({ items: [], next: null })),
    questions: vi.fn(async () => ({ items: [] })),
  } satisfies Record<keyof TasksClient, unknown>;
}

export type FakeTasksClient = ReturnType<typeof fakeTasksClient>;


export function Fresh({ children }: { children: ReactNode }) {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
}
