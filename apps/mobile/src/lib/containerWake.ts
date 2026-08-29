/**
 * Explicit container wake for mobile — the client half of wake-on-intent.
 *
 * POST /container/wake is the only client-triggerable wake for a parked trial
 * container. On mobile the intent signal is the app returning to the
 * foreground (`reason: "mount"`) — iOS only makes an app active on a
 * deliberate switch, so that IS presence (see presence.ts). Passive reasons
 * are gated on `isPresent()`; action reasons (message send happens
 * server-side; a Wake button would pass "interaction") bypass the gate.
 *
 * Every failure is a per-request soft-fail: an old backend 404s the route
 * (never retry-loop it), a gated/capped owner 402s (the status poll renders
 * the limit card), transient errors just wait for the next foreground. Status
 * polling owns the UX either way.
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useMemo, useRef } from "react";
import { createApiClient } from "@lightbulb/shared";

import { makeAdapter } from "./adapter";
import { isPresent } from "./presence";

export type WakeReason = "mount" | "interaction" | "message_send" | "retry" | "teams_wake";

const PASSIVE_REASONS: ReadonlySet<string> = new Set(["mount", "interaction"]);
const COOLDOWN_MS = 20_000;

// Module-level so multiple mounts of the hook share one wake budget/cooldown
// (mirrors the web hook — one app, one wake cadence).
let inFlight = false;
let lastAt = 0;

export function useContainerWake(): { requestWake: (reason: WakeReason) => Promise<void> } {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

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
      }
    },
    [api],
  );

  return { requestWake };
}
