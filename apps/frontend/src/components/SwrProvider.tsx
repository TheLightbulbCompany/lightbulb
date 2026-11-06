"use client";

import { useEffect } from "react";
import { useAuth } from "@clerk/nextjs";
import { SWRConfig } from "swr";
import { useResetSwrOnUserChange } from "@lightbulb/shared";
import { clearRoomsCache } from "@/lib/roomsCache";

/**
 * App-wide SWR defaults.
 *
 * Window-focus revalidation is off globally: the surfaces that need liveness
 * already get it from event-driven mutation (gateway events, Teams events) or
 * explicit polling, so refocusing the tab shouldn't re-fire every mounted
 * read — for the Teams BFF reads in particular each one is a proxied,
 * token-refreshed round trip. Hooks that genuinely want focus revalidation
 * opt back in per-call.
 *
 * Reconnect revalidation stays ON — deliberately the opposite call, stated
 * explicitly here rather than left to SWR's default. Focus fires constantly
 * (every tab switch, dozens of times an hour) and almost never coincides with
 * stale data. Reconnect fires rarely, only on a real network transition, and
 * marks the one moment when caches are most likely WRONG: a laptop waking
 * after hours asleep has missed every push event in the meantime, and the
 * reads that aren't event-driven (`/billing/account` has neither an interval
 * nor focus revalidation) have no other path back to fresh. The cost is a
 * one-shot burst on a rare edge; the idle request rate — what we actually
 * care about — is unaffected.
 */
export function SwrProvider({ children }: { children: React.ReactNode }) {
  // Cached reads are one viewer's: a sign-out or another account drops them all.
  const { isLoaded, userId } = useAuth();
  useResetSwrOnUserChange(userId);
  // Signed out: the room lists kept for a fast reload go too.
  useEffect(() => { if (isLoaded && !userId) clearRoomsCache(); }, [isLoaded, userId]);
  return (
    <SWRConfig value={{ revalidateOnFocus: false, revalidateOnReconnect: true }}>
      {children}
    </SWRConfig>
  );
}
