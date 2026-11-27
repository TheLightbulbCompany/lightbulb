/**
 * The single PostHog touchpoint for the frontend: `import "posthog-js"` appears only here, and every other
 * file imports from `@/lib/analytics`.
 *
 * The admin dashboard (apps/backend/core/services/ops/spark_metrics.py) reads exactly these web-originated
 * things, so they stay: the `spark_download_clicked` event (properties `source`, and `environment` plus the
 * automatic `$host`, which every query filters on), and the person properties `$initial_utm_source` and
 * `email`. Nothing else the website sends is read by anything.
 */

import posthog from "posthog-js";
import { PostHogProvider as PHProvider } from "posthog-js/react";

/** The PostHog client, re-exported so `components/PostHogProvider.tsx` can hand it to `<Provider>`. */
export const client = posthog;

/** The PostHog React provider, re-exported for the root layout. */
export const Provider = PHProvider;

/** Every product event the website sends. */
export type AnalyticsEvent = "spark_download_clicked";

function isLoaded(): boolean {
  return (posthog as unknown as { __loaded?: boolean }).__loaded === true;
}

// Clerk forwards its sign-in ticket to /sign-in: drop those params anywhere, keep the rest.
// redirect_url can carry a private deep link, so it goes too.
const SECRET_PARAMS = ["__clerk_ticket", "__clerk_status", "redirect_url"];

/** Strips Clerk's ticket params (and a /j fragment) from a URL before it leaves the browser. */
export function withoutChatQuery(value: string): string {
  try {
    const url = new URL(value, "https://thelightbulb.company");
    // /j carries a join ticket in its fragment: keep the route and query, drop the fragment.
    if (url.pathname === "/j") return value.split("#", 1)[0];
    if (!SECRET_PARAMS.some((key) => url.searchParams.has(key))) return value;
    for (const key of SECRET_PARAMS) url.searchParams.delete(key);
    const absolute = /^[a-z]+:\/\//i.test(value);
    const rest = `${url.pathname}${url.search}${url.hash}`;
    return absolute ? `${url.origin}${rest}` : rest;
  } catch {
    return value;
  }
}

/** Start PostHog. A no-op on the server. */
export function init(apiKey: string, options: Parameters<typeof posthog.init>[1]): void {
  if (typeof window === "undefined") return;
  posthog.init(apiKey, options);
}

/** Capture a product event. A no-op on the server and until PostHog has loaded (no key set). */
export function capture(event: AnalyticsEvent, properties?: Record<string, unknown>): void {
  if (typeof window === "undefined" || !isLoaded()) return;
  posthog.capture(event, properties);
}

/** Super properties, merged into every later event. `reset()` clears them, so re-register after it. */
export function register(properties: Record<string, unknown>): void {
  if (typeof window === "undefined" || !isLoaded()) return;
  posthog.register(properties);
}

/** Forward a browser error to PostHog's error tracking. */
export function captureException(error: unknown, properties?: Record<string, unknown>): void {
  if (typeof window === "undefined" || !isLoaded()) return;
  if (properties) posthog.captureException(error, properties);
  else posthog.captureException(error);
}

/** Bind the Clerk user id to this browser's PostHog person (this is what carries `$initial_utm_source`). */
export function identify(distinctId: string, properties?: Record<string, unknown>): void {
  if (typeof window === "undefined" || !isLoaded()) return;
  posthog.identify(distinctId, properties);
}

/** Clear the bound person on sign-out. */
export function reset(): void {
  if (typeof window === "undefined" || !isLoaded()) return;
  posthog.reset();
}
