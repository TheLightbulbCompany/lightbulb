"use client";

import { useEffect, useRef } from "react";
import { useApi } from "@/lib/api";

// /users/sync waits for the gate to be ``ready``. Firing sync with a
// pre-activation JWT used to create phantom personal billing rows — the
// backend no longer does that write, but we gate here too so other
// context-sensitive endpoints called from sync paths (future-proofing)
// don't misresolve owner_id either.
//
// Synchronous in-flight latch (`syncStartedRef`): this effect depends on
// `api`, which `useApi()` returns as a fresh object every render, so it
// re-runs on every re-render — and ChatLayout re-renders frequently while
// data hooks settle (e.g. `useProvisioningState` polling `/container/status`).
// The sessionStorage guard below is only written *after* the async
// `syncUser()` resolves, so during that in-flight window the re-render
// burst would re-fire `syncUser` several times (a real over-fetch, since
// sync is a network POST). The ref latches synchronously before the call
// so the effect fires sync at most once per mount; sessionStorage handles
// the cross-mount / cross-session dedupe. On failure we release the latch
// (sessionStorage is only written on success) so a genuine error can retry.
export function useUserSyncOnce(enabled: boolean): void {
  const api = useApi();
  // Per-mount latch so the /users/sync effect (which re-runs on every render
  // because `api` identity churns) fires the sync at most once. See the
  // rationale above for the full story.
  const syncStartedRef = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    if (syncStartedRef.current) return;
    // Once per browser session: sync is an idempotent Clerk→DDB upsert and
    // ChatLayout remounts on every view switch / page load — re-firing it
    // each time is pure backend load.
    const syncedKey = "isol8:user-synced";
    try {
      if (sessionStorage.getItem(syncedKey)) return;
    } catch {
      // sessionStorage unavailable (privacy mode) — fall through and sync.
    }
    syncStartedRef.current = true;
    api
      .syncUser()
      .then(() => {
        try {
          sessionStorage.setItem(syncedKey, "1");
        } catch {
          // best effort
        }
      })
      .catch((err: unknown) => {
        // Release the latch so the next render can retry — the session
        // guard is only set on success, so a failed sync must stay retryable.
        syncStartedRef.current = false;
        console.error("User sync failed:", err);
      });
  }, [enabled, api]);
}
