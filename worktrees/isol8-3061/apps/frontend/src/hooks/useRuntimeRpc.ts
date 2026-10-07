"use client";

import {
  useRuntimeRpc as useRuntimeRpcShared,
  useRuntimeRpcMutation as useRuntimeRpcMutationShared,
  type RpcResult,
} from "@lightbulb/shared";
import type { SWRConfiguration } from "swr";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export function useRuntimeRpc<T = unknown>(
  method: string | null,
  params?: Record<string, unknown>,
  config?: SWRConfiguration,
): RpcResult<T> {
  return useRuntimeRpcShared<T>(useLiveEvents(), method, params, config);
}

export function useRuntimeRpcMutation() {
  return useRuntimeRpcMutationShared(useLiveEvents());
}
