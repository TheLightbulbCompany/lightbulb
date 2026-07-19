"use client";

// Explicit container wake — the client half of wake-on-intent.
//
// POST /container/wake is the only client-triggerable wake for a parked trial
// container. Passive reasons (mount, interaction) are gated on presence
// (visible + input < 120s) so a display waking at 5am can never fire one;
// action reasons (message_send, retry, teams_wake) ARE the interaction and
// bypass the recency check by construction. Every failure is a per-request
// soft-fail: an old backend 404s the route (never cache that), a gated owner
// 402s (the status poll renders the limit card), transient errors just wait
// for the next trigger. Status polling owns the UX either way.

import { useCallback } from "react";
import { mutate } from "swr";

import { useApi } from "@/lib/api";
import { isPresent } from "@/lib/presence";

export type WakeReason = "mount" | "interaction" | "message_send" | "retry" | "teams_wake";

const PASSIVE_REASONS: ReadonlySet<string> = new Set(["mount", "interaction"]);
const COOLDOWN_MS = 20_000;

// Module-level (not per-hook-instance) dedupe: nested GatewayProviders exist
// (settings mounts one inside the layout's), and per-instance refs would let
// each fire its own mount wake. One page = one wake budget.
let inFlight = false;
let lastAt = 0;

export function useContainerWake(): { requestWake: (reason: WakeReason) => Promise<void> } {
  const api = useApi();

  const requestWake = useCallback(
    async (reason: WakeReason): Promise<void> => {
      if (PASSIVE_REASONS.has(reason) && !isPresent()) return;
      const now = Date.now();
      if (inFlight || now - lastAt < COOLDOWN_MS) return;
      inFlight = true;
      lastAt = now;
      try {
        await api.post("/container/wake", { reason });
      } catch {
        // Soft-fail by design — see module comment.
      } finally {
        inFlight = false;
        // Revalidate status regardless of outcome. The SWR key is stable by
        // design, so this reaches every useProvisioningState consumer — on
        // success the stepper sees provisioning->ready promptly; on a 402
        // the fetcher surfaces the limit card. Without this, a tab whose
        // reachedReady latch already zeroed its poll interval would render
        // stale ready/parked state forever after a rejected wake.
        void mutate("/container/status");
      }
    },
    [api],
  );

  return { requestWake };
}
