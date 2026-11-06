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

  it("outputs: kind repeats (Links = link + pr), the rest are plain params", async () => {
    const { api, get } = fakeApi();
    const c = createTasksClient(api);
    await c.outputs();
    await c.outputs({ kind: ["link", "pr"], roomId: "r 1", cursor: "n", limit: 4 });
    await c.outputs({ kind: "app", specialist: "growth" });
    expect(get.mock.calls.map((x) => (x as unknown[])[0])).toEqual([
      "/tasks/outputs", "/tasks/outputs?kind=link&kind=pr&roomId=r+1&cursor=n&limit=4", "/tasks/outputs?kind=app&specialist=growth",
    ]);
  });

  it("actions, answer, trace, read, needs-you", async () => {
    const { api, get, post } = fakeApi();
    const c = createTasksClient(api);
    await c.act("t", "stop", 4);
    await c.answer("t", "q", { optionId: "approve", clientAnswerId: "c", expectVersion: 2 });
    await c.markRead("t");
    await c.trace("t", 2);
    await c.get("t");
    await c.needsYou();
    await c.questions("t");
    expect(post.mock.calls).toEqual([
      ["/tasks/t/stop", { version: 4 }],
      ["/tasks/t/questions/q/answer", { optionId: "approve", clientAnswerId: "c", expectVersion: 2 }],
      ["/tasks/t/read", {}],
    ]);
    expect(get.mock.calls.map((x) => (x as unknown[])[0])).toEqual(["/tasks/t/trace?attempt=2", "/tasks/t", "/needs-you", "/tasks/t/questions"]);
    expect([...get.mock.calls, ...post.mock.calls].flat().join(" ")).not.toMatch(/apply/);
  });
});
