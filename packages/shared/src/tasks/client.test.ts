import { describe, expect, it, vi } from "vitest";

import type { ApiMethods } from "../api/client";
import { createTasksClient } from "./client";

function fakeApi() {
  const get = vi.fn(async () => ({}));
  const post = vi.fn(async () => ({}));
  return { api: { get, post } as unknown as ApiMethods, get, post };
}

describe("tasks client: routes and bodies (wave2-README C15)", () => {
  it("carries the workspace for cache keys", () => {
    expect(createTasksClient(fakeApi().api, "org_1").cacheScope).toBe("org_1");
  });

  it("list scopes", async () => {
    const { api, get } = fakeApi();
    const c = createTasksClient(api);
    await c.list({ kind: "room", roomId: "r1" });
    await c.list({ kind: "mine" }, "abc");
    await c.list({ kind: "workspace" });
    expect(get.mock.calls.map((x) => (x as unknown[])[0])).toEqual([
      "/tasks?scope=room%3Ar1", "/tasks?scope=mine&cursor=abc", "/tasks?scope=workspace",
    ]);
  });

  it("actions, answer, bulk start, trace, read, needs-you", async () => {
    const { api, get, post } = fakeApi();
    const c = createTasksClient(api);
    await c.act("t", "stop", 4);
    await c.answer("t", "q", { optionId: "approve", clientAnswerId: "c", expectVersion: 2 });
    await c.startMany(["a", "b"]);
    await c.markRead("t");
    await c.trace("t", 2);
    await c.get("t");
    await c.needsYou();
    await c.questions("t");
    expect(post.mock.calls).toEqual([
      ["/tasks/t/stop", { version: 4 }],
      ["/tasks/t/questions/q/answer", { optionId: "approve", clientAnswerId: "c", expectVersion: 2 }],
      ["/tasks/start", { taskIds: ["a", "b"] }],
      ["/tasks/t/read", {}],
    ]);
    expect(get.mock.calls.map((x) => (x as unknown[])[0])).toEqual(["/tasks/t/trace?attempt=2", "/tasks/t", "/needs-you", "/tasks/t/questions"]);
    expect([...get.mock.calls, ...post.mock.calls].flat().join(" ")).not.toMatch(/apply/);
  });
});
