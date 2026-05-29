// frontend/src/hooks/useGatewayRpc.ts
"use client";

import { useCallback, useEffect, useRef } from "react";
import useSWR, { SWRConfiguration } from "swr";
import { useGateway } from "@/hooks/useGateway";

interface RpcResult<T = unknown> {
  data: T | undefined;
  error: Error | undefined;
  isLoading: boolean;
  mutate: () => Promise<T | undefined>;
}

/**
 * Hook for read-only RPC calls via the gateway WebSocket (auto-fetched via SWR).
 *
 * Drop-in replacement for useContainerRpc. Same API, same return type.
 *
 * Usage:
 *   const { data, isLoading } = useGatewayRpc<HealthData>("health");
 *   const { data } = useGatewayRpc<AgentList>("agents.list");
 */
export function useGatewayRpc<T = unknown>(
  method: string | null,
  params?: Record<string, unknown>,
  config?: SWRConfiguration,
): RpcResult<T> {
  const { sendReq, onEvent } = useGateway();

  const fetcher = useCallback(
    async (key: string) => {
      // Split on first two "|" only — params JSON may contain pipe characters
      const firstPipe = key.indexOf("|");
      const secondPipe = key.indexOf("|", firstPipe + 1);
      const m = key.slice(firstPipe + 1, secondPipe);
      const paramStr = key.slice(secondPipe + 1);
      const parsedParams = paramStr ? JSON.parse(paramStr) : undefined;
      try {
        return (await sendReq(m, parsedParams)) as T;
      } catch (err) {
        // Match old behavior: 404-equivalent returns undefined
        if (err instanceof Error && err.message.includes("No container")) {
          return undefined;
        }
        throw err;
      }
    },
    [sendReq],
  );

  const swrKey = method
    ? `rpc|${method}|${params ? JSON.stringify(params) : ""}`
    : null;

  const { data, error, isLoading, mutate } = useSWR<T | undefined>(
    swrKey as string | null,
    fetcher as (key: string) => Promise<T | undefined>,
    {
      revalidateOnFocus: false,
      dedupingInterval: 10000,
      // SWR's default `onErrorRetry` does exponential backoff
      // (5s, 10s, 20s, 40s...). Bad for cold-start probing: a single
      // handshake-timeout on the gateway pool during the openclaw
      // sidecar window pushes the next probe minutes out, even though
      // the gateway becomes healthy ~10s later — user gets stuck on
      // the provisioning stepper waiting for the long-since-recovered
      // gateway. Retry at a fixed cadence instead, capped so we don't
      // poll forever on a permanently broken container.
      errorRetryInterval: 3000,
      onErrorRetry: (_err, _key, swrCfg, revalidate, opts) => {
        const retryCount = opts?.retryCount ?? 0;
        if (retryCount >= 100) return; // ~5 min at 3s
        const delay =
          typeof swrCfg.refreshInterval === "number" && swrCfg.refreshInterval > 0
            ? swrCfg.refreshInterval
            : swrCfg.errorRetryInterval ?? 3000;
        setTimeout(() => revalidate({ retryCount: retryCount + 1 }), delay);
      },
      ...config,
    },
  );

  // Auto-revalidate when gateway pushes a matching event (throttled to
  // respect dedupingInterval — without this, high-frequency OpenClaw events
  // bypass SWR deduping and flood the backend with RPCs).
  const lastRevalidateRef = useRef(0);
  useEffect(() => {
    if (!method) return;
    return onEvent((event) => {
      if (method === event || method.startsWith(event + ".")) {
        const now = Date.now();
        if (now - lastRevalidateRef.current < 10_000) return;
        lastRevalidateRef.current = now;
        mutate();
      }
    });
  }, [method, onEvent, mutate]);

  return {
    data,
    error: error as Error | undefined,
    isLoading,
    // Return SWR's mutate promise so callers can `await` a revalidation
    // and act only after the fresh data has been applied.
    mutate: () => mutate(),
  };
}

/**
 * Hook for write RPC calls via the gateway WebSocket (imperative, not auto-fetched).
 *
 * Drop-in replacement for useContainerRpcMutation.
 *
 * Usage:
 *   const callRpc = useGatewayRpcMutation();
 *   await callRpc("config.set", { key: "value" });
 */
export function useGatewayRpcMutation() {
  const { sendReq } = useGateway();

  return useCallback(
    async <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      timeoutMs?: number,
    ): Promise<T> => {
      return (await sendReq(method, params, timeoutMs)) as T;
    },
    [sendReq],
  );
}
