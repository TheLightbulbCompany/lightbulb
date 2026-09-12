import { describe, expect, it, vi } from "vitest";
import { createUsageHook } from "./usage.js";

const ENTRY = { model: "claude-sonnet-4-6", inputTokens: 12, outputTokens: 3, cacheRead: 50, cacheWrite: 1, estimatedCostUsd: 0.01 };
const CTX = { agentId: "friday", sessionKey: "agent:friday:cron:job-1" };

describe("createUsageHook", () => {
  it("posts usage for a non-room run", async () => {
    const post = vi.fn(async () => {});
    await createUsageHook({ post, getSessionEntry: () => ENTRY })({ runId: "run-1", success: true, durationMs: 1500 }, CTX);
    expect(post).toHaveBeenCalledWith("/api/v1/internal/messaging/usage", {
      sessionKey: CTX.sessionKey, agentId: "friday", runId: "run-1", success: true, durationMs: 1500,
      usage: { model: "claude-sonnet-4-6", inputTokens: 12, outputTokens: 3, cacheReadTokens: 50, cacheWriteTokens: 1, estimatedCostUsd: 0.01 },
    });
  });

  it("skips room runs and runs without ids", async () => {
    const post = vi.fn(async () => {});
    const hook = createUsageHook({ post, getSessionEntry: () => ENTRY });
    await hook({ runId: "r", success: true }, { agentId: "friday", sessionKey: "agent:friday:lightbulb:channel:22222222-2222-4222-8222-222222222222" });
    await hook({ success: true }, CTX);
    await hook({ runId: "r", success: true }, { sessionKey: CTX.sessionKey });
    expect(post).not.toHaveBeenCalled();
  });

  it("never rejects and never forwards the error text", async () => {
    const post = vi.fn(async () => { throw new Error("down"); });
    const log = vi.fn();
    await expect(createUsageHook({ post, getSessionEntry: () => undefined, log })({ runId: "r", success: false, error: "secret text" }, CTX)).resolves.toBeUndefined();
    expect(JSON.stringify(post.mock.calls)).not.toContain("secret text");
    expect(log).toHaveBeenCalledTimes(1);
  });
});
