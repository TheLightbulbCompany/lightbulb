"use client";

import { SWRConfig } from "swr";

/**
 * App-wide SWR defaults.
 *
 * Window-focus revalidation is off globally: the surfaces that need liveness
 * already get it from event-driven mutation (gateway events, Teams events) or
 * explicit polling, so refocusing the tab shouldn't re-fire every mounted
 * read — for the Teams BFF reads in particular each one is a proxied,
 * token-refreshed round trip. Hooks that genuinely want focus revalidation
 * opt back in per-call.
 */
export function SwrProvider({ children }: { children: React.ReactNode }) {
  return <SWRConfig value={{ revalidateOnFocus: false }}>{children}</SWRConfig>;
}
