import { useCallback, useEffect, useRef } from "react";
import useSWR, { type SWRConfiguration } from "swr";

import type { LiveEvent } from "../live/types";
import { RuntimeRpcError } from "../runtime/client";
import type { RuntimeOperation, RuntimeRpcOutcome } from "../runtime/types";

export interface RuntimeRpcSource {
  rpc<T = unknown>(method: string, params?: Record<string, unknown>): Promise<RuntimeRpcOutcome<T>>;
  getOperation(operationId: string): Promise<RuntimeOperation>;
  onEvent(handler: (event: LiveEvent) => void): () => void;
  connectionRevision: number;
}

const RESOURCE_METHOD_PREFIXES: Readonly<Record<string, readonly string[]>> = {
  agents: ["agents"],
  config: ["config"],
  models: ["models"],
  skills: ["skills"],
  tools: ["tools"],
  health: ["health"],
};

function eventMatchesMethod(method: string, event: LiveEvent): boolean {
  if (event.type !== "runtime.resource.invalidated") return false;
  if (event.payload.resource === "onboarding") return true;
  return (RESOURCE_METHOD_PREFIXES[event.payload.resource] ?? []).some(
    (prefix) => method === prefix || method.startsWith(`${prefix}.`),
  );
}

export interface RpcResult<T = unknown> {
  data: T | undefined;
  error: Error | undefined;
  isLoading: boolean;
  mutate: () => Promise<T | undefined>;
}

function runtimeRpcKey(method: string, params?: Record<string, unknown>): string {
  return `rpc|${method}|${params ? JSON.stringify(params) : ""}`;
}

const PUSH_REVALIDATE_MIN_MS = 10_000;
const lastPushRevalidateAt = new Map<string, number>();

function pushRevalidate(key: string, mutate: () => unknown): void {
  const now = Date.now();
  if (now - (lastPushRevalidateAt.get(key) ?? 0) < PUSH_REVALIDATE_MIN_MS) return;
  lastPushRevalidateAt.set(key, now);
  void mutate();
}

export function __resetPushRevalidateThrottle(): void {
  lastPushRevalidateAt.clear();
}

export function useRuntimeRpc<T = unknown>(
  runtime: RuntimeRpcSource,
  method: string | null,
  params?: Record<string, unknown>,
  config?: SWRConfiguration,
): RpcResult<T> {
  const { rpc, onEvent, connectionRevision } = runtime;
  const fetcher = useCallback(async (key: string) => {
    const firstPipe = key.indexOf("|");
    const secondPipe = key.indexOf("|", firstPipe + 1);
    const selectedMethod = key.slice(firstPipe + 1, secondPipe);
    const encodedParams = key.slice(secondPipe + 1);
    try {
      const outcome = await rpc<T>(selectedMethod, encodedParams ? JSON.parse(encodedParams) : undefined);
      if (outcome.kind === "operation") {
        throw new RuntimeRpcError({
          status: 500,
          code: "INTERNAL_ERROR",
          message: "Runtime read unexpectedly became asynchronous.",
          retryable: false,
        });
      }
      return outcome.value;
    } catch (error) {
      if (error instanceof Error && error.message.includes("No container")) return undefined;
      throw error;
    }
  }, [rpc]);

  const key = method ? runtimeRpcKey(method, params) : null;
  const { data, error, isLoading, isValidating, mutate } = useSWR<T | undefined>(key, fetcher, {
    revalidateOnFocus: false,
    dedupingInterval: 10_000,
    errorRetryInterval: 3_000,
    onErrorRetry: (nextError, _key, swrConfig, revalidate, options) => {
      if (nextError instanceof RuntimeRpcError && !nextError.retryable) return;
      const retryCount = options?.retryCount ?? 0;
      if (retryCount >= 100) return;
      const base = typeof swrConfig.refreshInterval === "number" && swrConfig.refreshInterval > 0
        ? swrConfig.refreshInterval
        : swrConfig.errorRetryInterval ?? 3_000;
      // Backs off rather than retrying flat. Callers that pass a FUNCTION for
      // `refreshInterval` (useAgents does, to stop polling once the roster
      // arrives) never matched the numeric branch, so they fell to a flat 3s —
      // and a container that is still cold fails every one of them, which made
      // an ordinary boot roughly five minutes of POST /runtime/rpc at full
      // rate. Capped so a long boot is still covered, just quietly.
      const delay = Math.min(base * 2 ** Math.min(retryCount, 5), 30_000);
      setTimeout(() => revalidate({ retryCount: retryCount + 1 }), delay);
    },
    ...config,
  });

  useEffect(() => {
    if (!method || !key) return;
    return onEvent((event) => {
      if (eventMatchesMethod(method, event)) pushRevalidate(key, mutate);
    });
  }, [method, key, onEvent, mutate]);

  // A reconnect refetches the read, after any read in flight lands: `mutate` would start a newer request and SWR
  // drops the older one's answer, so a cold load's first ack made every read wait for a second round trip (W14-4).
  const previousRevision = useRef(connectionRevision);
  const reconcileDue = useRef(false);
  useEffect(() => {
    if (connectionRevision > previousRevision.current) reconcileDue.current = true;
    previousRevision.current = connectionRevision;
    if (!key) { reconcileDue.current = false; return; }
    if (!reconcileDue.current || isValidating) return;
    reconcileDue.current = false;
    void mutate();
  }, [connectionRevision, key, isValidating, mutate]);

  return {
    data,
    error: error as Error | undefined,
    isLoading,
    mutate: () => mutate(),
  };
}

interface PendingMutation {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

function operationFailure(operation: RuntimeOperation): RuntimeRpcError {
  return new RuntimeRpcError({
    status: 500,
    code: operation.error?.code ?? "INTERNAL_ERROR",
    message: operation.error?.message ?? "Runtime operation failed.",
    retryable: operation.error?.retryable ?? false,
  });
}

export function useRuntimeRpcMutation(runtime: RuntimeRpcSource) {
  const pending = useRef(new Map<string, PendingMutation>());
  const checking = useRef(new Set<string>());

  const reconcile = useCallback(async (operationId: string) => {
    const waiter = pending.current.get(operationId);
    if (!waiter || checking.current.has(operationId)) return;
    checking.current.add(operationId);
    try {
      const operation = await runtime.getOperation(operationId);
      if (operation.state === "succeeded") {
        pending.current.delete(operationId);
        waiter.resolve(operation.result);
      } else if (operation.state === "failed") {
        pending.current.delete(operationId);
        waiter.reject(operationFailure(operation));
      }
    } catch (error) {
      if (error instanceof RuntimeRpcError && !error.retryable) {
        pending.current.delete(operationId);
        waiter.reject(error);
      }
    } finally {
      checking.current.delete(operationId);
    }
  }, [runtime]);

  useEffect(() => runtime.onEvent((event) => {
    if (event.type === "runtime.operation.changed") {
      void reconcile(event.payload.operationId);
    }
  }), [runtime, reconcile]);

  const previousRevision = useRef(runtime.connectionRevision);
  useEffect(() => {
    const previous = previousRevision.current;
    previousRevision.current = runtime.connectionRevision;
    if (runtime.connectionRevision <= previous) return;
    for (const operationId of pending.current.keys()) void reconcile(operationId);
  }, [runtime.connectionRevision, reconcile]);

  useEffect(() => () => {
    for (const waiter of pending.current.values()) {
      waiter.reject(new Error("Runtime operation cancelled because the signed-in workspace changed."));
    }
    pending.current.clear();
  }, []);

  return useCallback(async <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
  ): Promise<T> => {
    const outcome = await runtime.rpc<T>(method, params);
    if (outcome.kind === "result") return outcome.value;
    return await new Promise<T>((resolve, reject) => {
      pending.current.set(outcome.operationId, {
        resolve: (value) => resolve(value as T),
        reject,
      });
      void reconcile(outcome.operationId);
    });
  }, [runtime, reconcile]);
}
