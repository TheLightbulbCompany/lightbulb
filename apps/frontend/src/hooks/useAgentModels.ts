"use client";

import { useMemo } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
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
 * The owner's models for the active agent via OpenClaw `models.list` (per-agent
 * discovery). On timeout/empty we fall back to the agent's configured default
 * model from config.get so the picker never shows "no models" — see the design
 * doc's error-handling section.
 */
export function useAgentModels(agentId: string | null): UseAgentModelsResult {
  const { data, error, isLoading } = useGatewayRpc<ModelsListResponse | ModelEntry[]>(
    agentId ? "models.list" : null,
    agentId ? { agentId } : undefined,
  );
  const { data: cfg } = useGatewayRpc<ConfigGet>("config.get");

  return useMemo(() => {
    const raw: ModelEntry[] = Array.isArray(data)
      ? data
      : (data as ModelsListResponse | undefined)?.models ?? [];

    const agentEntry = cfg?.config?.agents?.list?.find((a) => a?.id === agentId);
    const defaultModel =
      primary(agentEntry?.model) ?? primary(cfg?.config?.agents?.defaults?.model) ?? "";

    if (error || raw.length === 0) {
      const fallback: ModelEntry[] = defaultModel
        ? [
            {
              id: defaultModel,
              provider: defaultModel.split("/")[0] || "unknown",
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
