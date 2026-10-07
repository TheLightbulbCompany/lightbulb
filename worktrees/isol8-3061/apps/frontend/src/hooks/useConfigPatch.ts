"use client";

import { useCallback } from "react";
import { useStatusLoaded } from "@lightbulb/shared";
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
 * `agentId` picks ONE Hermes profile: there is no single config document, so
 * `config.get`/`config.patch` without an id read a roster MAP and fan the
 * write out to every profile. That fan-out is right for an owner-level
 * setting and wrong for a per-agent one — a per-agent save used to write
 * into all six profiles and bump all six `.revision`s. Pass the agent's id
 * for a per-agent surface; omit it for an owner-level one.
 *
 * Both RPCs are held until a status body has landed for this workspace
 * (`useStatusLoaded() === false` = "not known for THIS workspace"):
 * guessing costs a ~55s microVM cold start.
 */
export function useConfigPatch(agentId?: string) {
  const statusLoaded = useStatusLoaded();
  const perProfile = statusLoaded && Boolean(agentId);
  // Per-profile reads ask for that profile by id, so `hash` is ITS hash — the map hash a
  // no-agentId read returns would never match the per-profile CAS the backend does.
  const { data: snapshot, mutate } = useRuntimeRpc<ConfigGetSnapshot>(
    statusLoaded ? "config.get" : null,
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
    // An owner-level read lands on the roster map. Every key such a surface reads
    // is owner-level and therefore identical across profiles, so the first one is the
    // representative — the patch still fans out to all of them.
    config: perProfile
      ? snapshot?.config
      : Object.values((snapshot?.config ?? {}) as Record<string, unknown>)[0],
    configReady: Boolean(baseHash),
    patchConfig,
    refreshConfig: mutate,
  };
}

/**
 * The owner's model list, from `models.list` — the owner's own Bedrock inference
 * profiles, served by the backend with no microVM wake, or Hermes's provider
 * catalog via the bridge on BYO/OAuth. `source` feeds
 * `buildModelNameMap`/`listCatalogModels`.
 *
 * One hook so every picker reads the catalog the same way. Held
 * (`source: undefined`) until the runtime is known for this workspace.
 */
export function useModelCatalog(): { source: unknown; defaultId?: string; ready: boolean } {
  const statusLoaded = useStatusLoaded();
  const { data } = useRuntimeRpc<{ config?: unknown; defaultId?: string }>(
    statusLoaded ? "models.list" : null,
  );
  if (!data) return { source: undefined, ready: false };
  return { source: data, defaultId: data.defaultId, ready: true };
}
