import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useTaskQuestions } from "@lightbulb/shared";
import { FIXTURES } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { useTaskActions } from "../useTaskActions";

describe("useTaskActions", () => {
  it("N-F1: Close re-reads the task's questions, so a cancelled approval loses its buttons", async () => {
    const task = FIXTURES.step08.details["t-signup"].task;
    const client = fakeTasksClient([task]);
    client.questions
      .mockResolvedValueOnce({ items: [{ id: "q1", state: "open" }] } as never)
      .mockResolvedValue({ items: [{ id: "q1", state: "cancelled" }] } as never);
    live.tasks = client;
    const { result } = renderHook(() => ({ q: useTaskQuestions(client as never, task.taskId), a: useTaskActions("pane") }), { wrapper: Fresh });
    await waitFor(() => expect(result.current.q.data?.items[0].state).toBe("open"));
    await act(async () => { await result.current.a.act(task, "resolve"); });
    await waitFor(() => expect(result.current.q.data?.items[0].state).toBe("cancelled"));
  });
});
