import { describe, expect, it, vi } from "vitest";

import type { ApiMethods } from "../api/client";
import { ApiError } from "../api/error";
import { createRuntimeClient, RuntimeRpcError } from "./client";

function setup(postResult: unknown = { ok: true, payload: { ready: true } }) {
  const methods = {
    get: vi.fn(async () => ({ operationId: "op-1", state: "succeeded", createdAt: "2026-09-10T00:00:00Z", updatedAt: "2026-09-10T00:00:01Z", result: {}, error: null })),
    post: vi.fn(async () => postResult),
  };
  const api = methods as unknown as ApiMethods;
  return { methods, client: createRuntimeClient(api, () => "11111111-1111-4111-8111-111111111111") };
}

describe("runtime HTTP client", () => {
  it("sends guarded RPC through HTTP with a fresh request id and unwraps synchronous payload", async () => {
    const { client, methods } = setup();
    await expect(client.rpc<{ ready: boolean }>("health", { verbose: false })).resolves.toEqual({
      kind: "result",
      value: { ready: true },
    });
    expect(methods.post).toHaveBeenCalledWith("/runtime/rpc", {
      requestId: "11111111-1111-4111-8111-111111111111",
      method: "health",
      params: { verbose: false },
    });
  });

  it("returns the durable operation handle and reads operation truth", async () => {
    const { client, methods } = setup({ operationId: "op-1" });
    await expect(client.rpc("agents.create", { name: "Gauge" })).resolves.toEqual({ kind: "operation", operationId: "op-1" });
    await expect(client.getOperation("op-1")).resolves.toMatchObject({ state: "succeeded" });
    expect(methods.get).toHaveBeenCalledWith("/runtime/operations/op-1");
  });

  it("preserves structured code, retryability, retryAfter seconds, and details without flattening", async () => {
    const body = {
      code: "RUNTIME_UNAVAILABLE",
      message: "Runtime is waking.",
      retryable: true,
      retryAfter: 3,
      details: { method: "health" },
    };
    const { client, methods } = setup();
    methods.post.mockRejectedValueOnce(new ApiError(503, body, body.message));
    const error = await client.rpc("health").catch((value) => value);
    expect(error).toBeInstanceOf(RuntimeRpcError);
    expect(error).toMatchObject({
      status: 503,
      code: "RUNTIME_UNAVAILABLE",
      message: "Runtime is waking.",
      retryable: true,
      retryAfter: 3,
      details: { method: "health" },
    });
    expect(JSON.stringify(error)).not.toContain("Bearer");
  });

  it("uses dedicated approval and question resolution endpoints", async () => {
    const { client, methods } = setup(null);
    await client.resolveApproval("approval-1", { decision: "allow-once", kind: "exec" });
    await client.resolveQuestion("question-1", { answers: { answers: { choice: ["yes"] } } });
    expect(methods.post).toHaveBeenNthCalledWith(1, "/runtime/approvals/approval-1/resolve", { decision: "allow-once", kind: "exec" });
    expect(methods.post).toHaveBeenNthCalledWith(2, "/runtime/questions/question-1/resolve", { answers: { answers: { choice: ["yes"] } } });
  });

  it("rejects malformed success bodies instead of leaking server payloads", async () => {
    const { client } = setup({ ok: true });
    const error = await client.rpc("health").catch((value) => value);
    expect(error).toBeInstanceOf(RuntimeRpcError);
    expect(error).toMatchObject({ code: "INTERNAL_ERROR", retryable: false });
    expect(error.message).toBe("Runtime returned an invalid response.");
  });
});
