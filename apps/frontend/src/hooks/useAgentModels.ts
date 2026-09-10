"use client";

import { useMemo } from "react";
import { useRuntimeRpc } from "@/hooks/useRuntimeRpc";
import type { ModelEntry } from "@/lib/models";

interface ModelsListResponse {
  models?: ModelEntry[];
}

interface ConfigGet {
  config?: {
    agents?: {
      defaults?: { model?: string | { primary?: string } };
      list?: { id?: string; model?: string | { primary?: string } }[];
    };
  };
}

function primary(model?: string | { primary?: string }): string | undefined {
  if (typeof model === "string") return model.trim() || undefined;
  if (model && typeof model === "object") return model.primary?.trim() || undefined;
  return undefined;
}

export interface UseAgentModelsResult {
  models: ModelEntry[];
  /** Model ref (`${provider}/${id}`) to fall back to; also the "Included" hint. */
  defaultModel: string;
  isLoading: boolean;
  usingFallback: boolean;
}

/**
 * The owner's models via OpenClaw `models.list`. On timeout/empty we fall back
 * to the active agent's configured default
 * model from config.get so the picker never shows "no models" — see the design
 * doc's error-handling section.
 */
export function useAgentModels(agentId: string | null): UseAgentModelsResult {
  // `view: "configured"` + `agentId`, the request the upstream Control UI
  // sends (ui/src/lib/model-catalog-store.ts). Without a view the gateway
  // serves `default`, which resolves against the STATIC plugin manifest and
  // races account-scoped discovery at 750ms, returning the stale static list
  // on timeout -- and SWR here never revalidates, so the tab keeps it. The
  // `configured` view waits for discovery and re-adds the agent's pinned
  // model so it never vanishes from the picker. Discovery itself is enabled
  // by agents.defaults.modelPolicy.allow (backend config.model_policy_block).
  const { data, error, isLoading } = useRuntimeRpc<ModelsListResponse | ModelEntry[]>(
    agentId ? "models.list" : null,
    agentId ? { view: "configured", agentId } : undefined,
  );
  const { data: cfg } = useRuntimeRpc<ConfigGet>("config.get");

  return useMemo(() => {
    const raw: ModelEntry[] = Array.isArray(data)
      ? data
      : (data as ModelsListResponse | undefined)?.models ?? [];

    const agentEntry = cfg?.config?.agents?.list?.find((a) => a?.id === agentId);
    const defaultModel =
      primary(agentEntry?.model) ?? primary(cfg?.config?.agents?.defaults?.model) ?? "";

    if (error || raw.length === 0) {
      // `defaultModel` is a full `provider/id` ref. The pill rebuilds the ref
      // via buildModelRef(entry.provider, entry.id), so the entry's `id` MUST
      // be the BARE id — passing the full ref double-prefixes it
      // (openai/openai/gpt-5, and corrupts Bedrock ARNs). Strip the leading
      // `provider/` segment; Bedrock ids are ARNs with internal slashes, so we
      // slice past the FIRST separator only.
      const provider = defaultModel.split("/")[0] || "unknown";
      const bareId = defaultModel.slice(provider.length + 1) || defaultModel;
      const fallback: ModelEntry[] = defaultModel
        ? [
            {
              id: bareId,
              provider,
              name: defaultModel,
              available: true,
            },
          ]
        : [];
      return { models: fallback, defaultModel, isLoading, usingFallback: true };
    }
    return { models: raw, defaultModel, isLoading, usingFallback: false };
  }, [data, cfg, agentId, error, isLoading]);
}
