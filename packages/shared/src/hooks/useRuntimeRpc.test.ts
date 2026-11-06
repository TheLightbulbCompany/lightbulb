// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RuntimeRpcError } from "../runtime/client";
import type { LiveEvent } from "../live/types";
import {
  __resetPushRevalidateThrottle,
  useRuntimeRpc,
  type RuntimeRpcSource,
} from "./useRuntimeRpc";

function event(resource: "agents" | "config" | "models" | "skills" | "tools" | "health" | "onboarding"): LiveEvent {
  return {
    schemaVersion: 1,
    eventId: `event-${resource}`,
    type: "runtime.resource.invalidated",
    occurredAt: "2026-09-10T00:00:00Z",
    payload: { resource },
  };
}

function makeSource(): RuntimeRpcSource & { emit: (event: LiveEvent) => void } {
  const handlers = new Set<(event: LiveEvent) => void>();
  return {
    connectionRevision: 1,
    rpc: vi.fn(async () => ({ kind: "result" as const, value: { ok: true } })) as unknown as RuntimeRpcSource["rpc"],
    getOperation: vi.fn(),
    onEvent(handler) {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
    emit(value) {
      for (const handler of handlers) handler(value);
    },
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetPushRevalidateThrottle();
});

describe("useRuntimeRpc", () => {
  it.each([
    ["health", "health"],
    ["config.get", "config"],
    ["agents.list", "agents"],
    ["models.list", "models"],
    ["skills.status", "skills"],
    ["tools.catalog", "tools"],
  ] as const)("revalidates %s from the %s resource event", async (method, resource) => {
    const source = makeSource();
    renderHook(() => useRuntimeRpc(source, method));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(1));
    source.emit(event(resource));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(2));
  });

  it("revalidates all runtime reads on onboarding and once after reconnect", async () => {
    const source = makeSource();
    const { rerender } = renderHook(() => useRuntimeRpc(source, "commands.list"));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(1));
    source.emit(event("onboarding"));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(2));
    source.connectionRevision = 2;
    rerender();
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(3));
  });

  it("keeps a read in flight when the live ack lands, then refetches (W14-4)", async () => {
    const source = makeSource();
    let first!: (value: unknown) => void;
    source.rpc = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { first = resolve; }))
      .mockResolvedValue({ kind: "result", value: { n: 2 } }) as unknown as RuntimeRpcSource["rpc"];
    const { result, rerender } = renderHook(() => useRuntimeRpc(source, "commands.list", { case: "w14-ack" }));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(1));
    source.connectionRevision = 2;
    rerender();
    expect(source.rpc).toHaveBeenCalledTimes(1);
    await act(async () => first({ kind: "result", value: { n: 1 } }));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.data).toEqual({ n: 2 }));
  });

  it("keeps No container as an empty success and rejects operation handles from read methods", async () => {
    const source = makeSource();
    source.rpc = vi.fn()
      .mockRejectedValueOnce(new Error("No container for user"))
      .mockResolvedValueOnce({ kind: "operation", operationId: "op-1" });
    const { result } = renderHook(() => useRuntimeRpc(source, "sessions.list", undefined, { shouldRetryOnError: false }));
    await waitFor(() => expect(source.rpc).toHaveBeenCalledTimes(1));
    expect(result.current.data).toBeUndefined();
    await act(async () => result.current.mutate());
    expect(result.current.error?.message).toBe("Runtime read unexpectedly became asynchronous.");
  });

  it("does not retry a structured non-retryable response", async () => {
    vi.useFakeTimers();
    const source = makeSource();
    source.rpc = vi.fn().mockRejectedValue(new RuntimeRpcError({
      status: 400,
      code: "INVALID_REQUEST",
      message: "agentId requires sessionKeys",
      retryable: false,
    }));
    renderHook(() => useRuntimeRpc(source, "sessions.search", { q: "terminal" }));
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(source.rpc).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
