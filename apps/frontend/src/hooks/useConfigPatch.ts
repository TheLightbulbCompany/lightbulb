"use client";

import { useCallback } from "react";
import { useRuntimeHost } from "@lightbulb/shared";
import { useRuntimeRpc, useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";

type ConfigGetSnapshot = {
  config?: unknown;
  hash?: string;
  valid?: boolean;
};

export type ConfigPatchResult = {
  ok?: boolean;
  /** True when the gateway restarts to apply the change — the chat WS drops
   *  for a few seconds and reconnects automatically. Surfaces should tell
   *  the user instead of looking broken. */
  restart?: boolean;
};

/**
 * Targeted config writes via the runtime's `config.patch` RPC.
 *
 * Mirrors the mechanics of upstream's config editor (controllers/config.ts):
 * `raw` is a JSON merge-patch STRING applied with `mergeObjectArraysById`
 * (so `agents.list: [{id, …}]` merges into the matching entry without
 * touching siblings; `null` deletes a key — RFC 7386 semantics), and
 * `baseHash` from the latest `config.get` makes the write optimistic-
 * concurrency-safe: a concurrent edit changes the hash and the patch is
 * rejected instead of clobbering.
 *
 * `agentId` (M6 task 10-C, scan rows 1/5) picks ONE Hermes profile on an
 * AgentCore workspace: there is no single `openclaw.json` there, so
 * `config.get`/`config.patch` without an id read a roster MAP and fan the
 * write out to every profile. That fan-out is right for an owner-level
 * setting and wrong for a per-agent one — a per-agent save used to write
 * into all six profiles and bump all six `.revision`s. Pass the agent's id
 * for a per-agent surface; omit it for an owner-level one.
 *
 * Both RPCs are held while the runtime host is unknown (`undefined` =
 * "not known for THIS workspace", never "assume ECS") — the same policy
 * `AgentToolsTab`/`useSystemHealth` follow, for the same reason: guessing
 * costs a ~55s microVM cold start.
 */
export function useConfigPatch(agentId?: string) {
  const runtimeHost = useRuntimeHost();
  const perProfile = runtimeHost === "agentcore" && Boolean(agentId);
  // Per-profile reads ask for that profile by id, so `hash` is ITS hash — the map hash a
  // no-agentId read returns would never match the per-profile CAS the backend does.
  const { data: snapshot, mutate } = useRuntimeRpc<ConfigGetSnapshot>(
    runtimeHost ? "config.get" : null,
    perProfile ? { agentId } : undefined,
  );
  const callRpc = useRuntimeRpcMutation();
  const baseHash = snapshot?.hash;

  const patchConfig = useCallback(
    async (patch: Record<string, unknown>): Promise<ConfigPatchResult> => {
      if (!baseHash) {
        throw new Error("Config not loaded yet — try again in a moment");
      }
      const res = (await callRpc("config.patch", {
        raw: JSON.stringify(patch),
        baseHash,
        ...(perProfile ? { agentId } : {}),
      })) as ConfigPatchResult;
      // Re-read so every consumer of config.get (model selectors, routing
      // cards) sees the new state and the next patch carries the new hash.
      // BEST-EFFORT: a restart-triggering patch can close the WebSocket
      // right after the write lands, so this follow-up read may reject
      // ("WebSocket closed") even though the patch SUCCEEDED — that must
      // not surface as a failure to the caller. SWR revalidates config.get
      // after the gateway reconnects anyway.
      try {
        await mutate();
      } catch {
        // Patch already applied; the refresh retries on reconnect.
      }
      return res;
    },
    [agentId, baseHash, callRpc, mutate, perProfile],
  );

  return {
    // An owner-level read on AgentCore lands on the roster map. Every key such a surface reads
    // is owner-level and therefore identical across profiles, so the first one is the
    // representative — the patch still fans out to all of them.
    config:
      runtimeHost === "agentcore" && !perProfile
        ? Object.values((snapshot?.config ?? {}) as Record<string, unknown>)[0]
        : snapshot?.config,
    configReady: Boolean(baseHash),
    patchConfig,
    refreshConfig: mutate,
  };
}

/**
 * The owner's model list, from whichever RPC carries it on this runtime (M6 task 10-C, rows
 * 2/9/23/27/31). AgentCore answers `models.list` — the owner's own Bedrock inference profiles,
 * served by the backend with no microVM wake, or Hermes's provider catalog via the bridge on
 * BYO/OAuth. ECS answers with the gateway config's providers catalog. `source` feeds
 * `buildModelNameMap`/`listCatalogModels`, which take either shape.
 *
 * One hook so the "which RPC holds the catalog" decision lives in one place rather than in
 * every picker. Held (`source: undefined`) while the runtime host is unknown.
 */
export function useModelCatalog(): { source: unknown; defaultId?: string; ready: boolean } {
  const runtimeHost = useRuntimeHost();
  const method = runtimeHost === "agentcore" ? "models.list" : runtimeHost === "ecs" ? "config.get" : null;
  const { data } = useRuntimeRpc<{ config?: unknown; defaultId?: string }>(method);
  if (!data) return { source: undefined, ready: false };
  return runtimeHost === "agentcore"
    ? { source: data, defaultId: data.defaultId, ready: true }
    : { source: data.config, ready: true };
}
