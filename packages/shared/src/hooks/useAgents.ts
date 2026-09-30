import { useCallback, useEffect, useMemo, useRef } from "react";

import { RuntimeRpcError } from "../runtime/client";
import type { RuntimeOperation } from "../runtime/types";
import {
  useRuntimeRpc,
  type RuntimeRpcSource,
} from "./useRuntimeRpc";
import type { AgentsListResponse } from "./agents";

export type AgentsRuntimeSource = RuntimeRpcSource;

export type AgentsAnalyticsEvent =
  | "agent_created"
  | "agent_deleted"
  | "agent_model_changed";

export interface UseAgentsOptions {
  capture?: (event: AgentsAnalyticsEvent, props?: Record<string, unknown>) => void;
}

const EMPTY_ROSTER_POLL_MS = 10_000;
function rosterRefreshInterval(latest: AgentsListResponse | undefined): number {
  return (latest?.agents?.length ?? 0) === 0 ? EMPTY_ROSTER_POLL_MS : 0;
}

interface PendingOperation {
  resolve: () => void;
  reject: (error: Error) => void;
}

function operationError(operation: RuntimeOperation): RuntimeRpcError {
  return new RuntimeRpcError({
    status: 500,
    code: operation.error?.code ?? "INTERNAL_ERROR",
    message: operation.error?.message ?? "Runtime operation failed.",
    retryable: operation.error?.retryable ?? false,
  });
}

export function useAgents(runtime: AgentsRuntimeSource, opts?: UseAgentsOptions) {
  const { data, error, isLoading, mutate } = useRuntimeRpc<AgentsListResponse>(
    runtime,
    "agents.list",
    undefined,
    { refreshInterval: rosterRefreshInterval },
  );
  const captureRef = useRef(opts?.capture);
  captureRef.current = opts?.capture;
  const pending = useRef(new Map<string, PendingOperation>());
  const checking = useRef(new Set<string>());

  const track = useCallback((event: AgentsAnalyticsEvent, props?: Record<string, unknown>) => {
    captureRef.current?.(event, props);
  }, []);

  const agents = useMemo(() => data?.agents ?? [], [data]);
  const defaultId = data?.defaultId;

  const reconcileOperation = useCallback(async (operationId: string) => {
    const waiter = pending.current.get(operationId);
    if (!waiter || checking.current.has(operationId)) return;
    checking.current.add(operationId);
    try {
      const operation = await runtime.getOperation(operationId);
      if (operation.state === "succeeded") {
        pending.current.delete(operationId);
        waiter.resolve();
      } else if (operation.state === "failed") {
        pending.current.delete(operationId);
        waiter.reject(operationError(operation));
      }
    } catch (nextError) {
      if (nextError instanceof RuntimeRpcError && !nextError.retryable) {
        pending.current.delete(operationId);
        waiter.reject(nextError);
      }
    } finally {
      checking.current.delete(operationId);
    }
  }, [runtime]);

  useEffect(() => runtime.onEvent((event) => {
    if (event.type === "runtime.operation.changed" && pending.current.has(event.payload.operationId)) {
      void reconcileOperation(event.payload.operationId);
    }
  }), [runtime, reconcileOperation]);

  const previousRevision = useRef(runtime.connectionRevision);
  useEffect(() => {
    const previous = previousRevision.current;
    previousRevision.current = runtime.connectionRevision;
    if (runtime.connectionRevision <= previous) return;
    for (const operationId of pending.current.keys()) void reconcileOperation(operationId);
  }, [runtime.connectionRevision, reconcileOperation]);

  useEffect(() => () => {
    for (const waiter of pending.current.values()) {
      waiter.reject(new Error("Runtime operation cancelled because the signed-in workspace changed."));
    }
    pending.current.clear();
  }, []);

  const waitForOperation = useCallback((operationId: string) => new Promise<void>((resolve, reject) => {
    pending.current.set(operationId, { resolve, reject });
    void reconcileOperation(operationId);
  }), [reconcileOperation]);

  const createAgent = useCallback(async (params: { name: string; emoji?: string }) => {
    // No `workspace`: the backend fills the layout for the owner's runtime (an EFS
    // path on ECS, nothing on Hermes/AgentCore) — see _sanitize_agent_create.
    try {
      const outcome = await runtime.rpc("agents.create", params);
      if (outcome.kind === "operation") await waitForOperation(outcome.operationId);
      track("agent_created");
    } finally {
      await mutate();
    }
  }, [runtime, waitForOperation, mutate, track]);

  const deleteAgent = useCallback(async (agentId: string) => {
    await runtime.rpc("agents.delete", { agentId, deleteFiles: true });
    track("agent_deleted", { agent_id: agentId });
    void mutate();
  }, [runtime, mutate, track]);

  // Names and runtime settings share the same identity-based mutation path;
  // Paperclip mirroring is an atomic concern of the backend RPC service.
  const updateAgent = useCallback(async (agentId: string, updates: { model?: string; name?: string }) => {
    await runtime.rpc("agents.update", { agentId, ...updates });
    if (updates.model) track("agent_model_changed", { agent_id: agentId, model: updates.model });
    void mutate();
  }, [runtime, mutate, track]);

  return {
    agents,
    defaultId,
    isLoading,
    isSettled: data !== undefined,
    error,
    refresh: () => mutate(),
    createAgent,
    deleteAgent,
    updateAgent,
  };
}
