// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveEvent } from "../live/types";
import type { RuntimeOperation } from "../runtime/types";

const mutate = vi.fn(async () => undefined);
let rpcData: { agents?: { id: string }[]; defaultId?: string } | undefined;
let lastRpcConfig: { refreshInterval?: unknown } | undefined;
vi.mock("./useRuntimeRpc", () => ({
  useRuntimeRpc: (
    _runtime: unknown,
    _method: string,
    _params: unknown,
    config: { refreshInterval?: unknown } | undefined,
  ) => {
    lastRpcConfig = config;
    return { data: rpcData, error: undefined, isLoading: false, mutate };
  },
}));

import { useAgents, type AgentsRuntimeSource } from "./useAgents";

const handlers = new Set<(event: LiveEvent) => void>();
const rpc = vi.fn();
const getOperation = vi.fn();
let connectionRevision = 1;

function runtime(): AgentsRuntimeSource {
  return {
    rpc,
    getOperation,
    connectionRevision,
    onEvent(handler) {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
  };
}

function operation(state: RuntimeOperation["state"], overrides: Partial<RuntimeOperation> = {}): RuntimeOperation {
  return {
    operationId: "op-1",
    state,
    createdAt: "2026-09-10T00:00:00Z",
    updatedAt: "2026-09-10T00:00:01Z",
    result: null,
    error: null,
    ...overrides,
  };
}

function emitOperation(state: RuntimeOperation["state"]) {
  const event: LiveEvent = {
    schemaVersion: 1,
    eventId: `event-${state}`,
    type: "runtime.operation.changed",
    occurredAt: "2026-09-10T00:00:01Z",
    payload: { operationId: "op-1", state, updatedAt: "2026-09-10T00:00:01Z", result: null, error: null },
  };
  for (const handler of handlers) handler(event);
}

beforeEach(() => {
  mutate.mockClear();
  rpc.mockReset();
  getOperation.mockReset();
  handlers.clear();
  rpcData = undefined;
  lastRpcConfig = undefined;
  connectionRevision = 1;
});

afterEach(cleanup);

describe("useAgents", () => {
  it("keeps agents.list polling only until the canonical roster arrives", () => {
    renderHook(() => useAgents(runtime()));
    const refreshInterval = lastRpcConfig?.refreshInterval as (latest: { agents?: unknown[] } | undefined) => number;
    expect(refreshInterval(undefined)).toBe(10_000);
    expect(refreshInterval({ agents: [] })).toBe(10_000);
    expect(refreshInterval({ agents: [{ id: "agent-ceo" }] })).toBe(0);
  });

  it("reports isSettled only after the runtime read resolves", () => {
    const first = renderHook(() => useAgents(runtime()));
    expect(first.result.current.isSettled).toBe(false);
    first.unmount();
    rpcData = { agents: [], defaultId: undefined };
    const second = renderHook(() => useAgents(runtime()));
    expect(second.result.current.isSettled).toBe(true);
  });

  it("waits for an async agents.create operation, then refreshes and records success", async () => {
    const capture = vi.fn();
    rpc.mockResolvedValue({ kind: "operation", operationId: "op-1" });
    getOperation.mockResolvedValueOnce(operation("running")).mockResolvedValueOnce(operation("succeeded", { result: { id: "gauge" } }));
    const { result } = renderHook(() => useAgents(runtime(), { capture }));
    let created!: Promise<void>;
    act(() => {
      created = result.current.createAgent({ name: "Gauge" });
    });
    await waitFor(() => expect(getOperation).toHaveBeenCalledWith("op-1"));
    act(() => emitOperation("succeeded"));
    await act(async () => created);
    expect(rpc).toHaveBeenCalledWith("agents.create", { name: "Gauge" });
    expect(capture).toHaveBeenCalledWith("agent_created", undefined);
    expect(mutate).toHaveBeenCalled();
  });

  it("reconciles a pending create from durable operation truth after reconnect", async () => {
    rpc.mockResolvedValue({ kind: "operation", operationId: "op-1" });
    getOperation.mockResolvedValueOnce(operation("queued")).mockResolvedValueOnce(operation("succeeded"));
    const { result, rerender } = renderHook(() => useAgents(runtime()));
    let created!: Promise<void>;
    act(() => {
      created = result.current.createAgent({ name: "Gauge" });
    });
    await waitFor(() => expect(getOperation).toHaveBeenCalledTimes(1));
    connectionRevision = 2;
    rerender();
    await act(async () => created);
    expect(getOperation).toHaveBeenCalledTimes(2);
    expect(mutate).toHaveBeenCalled();
  });
});
