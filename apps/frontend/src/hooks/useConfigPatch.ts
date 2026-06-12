"use client";

import { useCallback } from "react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";

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
 * Targeted config writes via OpenClaw's `config.patch` RPC.
 *
 * Mirrors the mechanics of upstream's config editor (controllers/config.ts):
 * `raw` is a JSON merge-patch STRING applied with `mergeObjectArraysById`
 * (so `agents.list: [{id, …}]` merges into the matching entry without
 * touching siblings; `null` deletes a key — RFC 7386 semantics), and
 * `baseHash` from the latest `config.get` makes the write optimistic-
 * concurrency-safe: a concurrent edit changes the hash and the patch is
 * rejected instead of clobbering.
 */
export function useConfigPatch() {
  const { data: snapshot, mutate } = useGatewayRpc<ConfigGetSnapshot>("config.get");
  const callRpc = useGatewayRpcMutation();
  const baseHash = snapshot?.hash;

  const patchConfig = useCallback(
    async (patch: Record<string, unknown>): Promise<ConfigPatchResult> => {
      if (!baseHash) {
        throw new Error("Config not loaded yet — try again in a moment");
      }
      const res = (await callRpc("config.patch", {
        raw: JSON.stringify(patch),
        baseHash,
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
    [baseHash, callRpc, mutate],
  );

  return {
    config: snapshot?.config,
    configReady: Boolean(baseHash),
    patchConfig,
    refreshConfig: mutate,
  };
}
