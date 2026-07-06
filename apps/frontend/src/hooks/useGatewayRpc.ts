// frontend/src/hooks/useGatewayRpc.ts
"use client";

import type { SWRConfiguration } from "swr";
import {
  useGatewayRpc as useGatewayRpcShared,
  useGatewayRpcMutation as useGatewayRpcMutationShared,
  type RpcResult,
} from "@isol8/shared";
import { useGateway } from "@/hooks/useGateway";

// Implementation lives in @isol8/shared (Phase 1 extraction — see the
// mobile-app plan); this wrapper binds the web gateway context so feature
// code keeps its historical no-arg call signature.

/**
 * Hook for read-only RPC calls via the gateway WebSocket (auto-fetched via SWR).
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
  return useGatewayRpcShared<T>(useGateway(), method, params, config);
}

/**
 * Hook for write RPC calls via the gateway WebSocket (imperative, not auto-fetched).
 *
 * Usage:
 *   const callRpc = useGatewayRpcMutation();
 *   await callRpc("config.set", { key: "value" });
 */
export function useGatewayRpcMutation() {
  return useGatewayRpcMutationShared(useGateway());
}
