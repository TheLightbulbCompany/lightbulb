// frontend/src/hooks/useRuntimeRpc.ts
"use client";

import type { SWRConfiguration } from "swr";
import {
  useRuntimeRpc as useRuntimeRpcShared,
  useRuntimeRpcMutation as useRuntimeRpcMutationShared,
  type RpcResult,
} from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

// Implementation lives in @lightbulb/shared; this wrapper binds the web live
// context so existing feature code keeps its historical no-arg call signature.

/**
 * Hook for read-only runtime RPC calls over HTTP (auto-fetched via SWR).
 *
 * Usage:
 *   const { data, isLoading } = useRuntimeRpc<HealthData>("health");
 *   const { data } = useRuntimeRpc<AgentList>("agents.list");
 */
export function useRuntimeRpc<T = unknown>(
  method: string | null,
  params?: Record<string, unknown>,
  config?: SWRConfiguration,
): RpcResult<T> {
  return useRuntimeRpcShared<T>(useLiveEvents(), method, params, config);
}

/**
 * Hook for imperative runtime RPC calls over HTTP.
 *
 * Usage:
 *   const callRpc = useRuntimeRpcMutation();
 *   await callRpc("config.set", { key: "value" });
 */
export function useRuntimeRpcMutation() {
  return useRuntimeRpcMutationShared(useLiveEvents());
}
