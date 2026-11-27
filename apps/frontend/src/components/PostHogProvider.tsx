"use client";

import { useEffect, useRef } from "react";
import { useAuth, useUser } from "@clerk/nextjs";

import {
  Provider as AnalyticsProvider,
  captureException,
  client as posthogClient,
  identify,
  init as initAnalytics,
  register,
  reset,
  withoutChatQuery,
} from "@/lib/analytics";

const POSTHOG_KEY = process.env.NEXT_PUBLIC_POSTHOG_KEY;
// Route PostHog through our own domain via the Next rewrite in
// next.config.ts ("/ingest/*" → us.i.posthog.com). Same-origin requests
// bypass ad-blockers / privacy extensions that otherwise drop every
// posthog call with ERR_BLOCKED_BY_CLIENT and flood the console with
// retries. `ui_host` keeps links in the PostHog dashboard pointed at
// the real PostHog UI.
const POSTHOG_API_HOST = "/ingest";
const POSTHOG_UI_HOST = "https://us.posthog.com";
const POSTHOG_URL_KEYS = [
  "$current_url", "$referrer", "$session_entry_url", "$session_entry_referrer",
  "$initial_current_url", "$initial_referrer",
  "first_touch_landing_path", "last_touch_landing_path",
  "first_touch_referrer", "last_touch_referrer",
] as const;

/**
 * The PostHog project is shared by dev, prod and the launch site, so every
 * client event carries `environment`. It comes from the page's own host, not
 * the build: a dev host can serve a prod-built bundle (both are Vercel
 * production domains), which once tagged dev pageviews `prod`.
 */
export function resolveEnvironment(hostname: string | undefined): "prod" | "launch" | "dev" {
  const host = (hostname ?? "").toLowerCase();
  // admin.thelightbulb.company is the prod admin dashboard; the readout's $host filter keeps it
  // out of the product funnel.
  if (host === "thelightbulb.company" || host === "www.thelightbulb.company" || host === "admin.thelightbulb.company") {
    return "prod";
  }
  // The launch site and its dev twin (LAUNCH_HOSTS): its own line, never the product funnel.
  if (host === "flickmeon.ai" || host.endsWith(".flickmeon.ai")) return "launch";
  return "dev";
}

const currentEnvironment = () =>
  resolveEnvironment(typeof window === "undefined" ? undefined : window.location.hostname);

/**
 * Initialise PostHog on first client mount.
 *
 * Previously called at module-evaluation time, which ran during SSR
 * import (no-op due to the `typeof window` guard) AND re-fired on every
 * Fast Refresh hot reload during dev — wasted work and a hidden cost on
 * first import even when PostHog is disabled at runtime. Routed through
 * `useEffect` + an `initializedRef` so the init runs exactly once on the
 * client.
 */
function PostHogInit() {
  const initializedRef = useRef(false);
  useEffect(() => {
    if (initializedRef.current || !POSTHOG_KEY) return;
    initializedRef.current = true;
    initAnalytics(POSTHOG_KEY, {
      api_host: POSTHOG_API_HOST,
      ui_host: POSTHOG_UI_HOST,
      person_profiles: "identified_only",
      capture_pageview: false,
      // Nothing reads $autocapture or $pageleave. $initial_utm_source needs neither: posthog-js keeps the
      // landing URL's UTM params in its persistence store and sends them on identify
      // (posthog.com/docs/data/anonymous-vs-identified-events).
      autocapture: false,
      capture_pageleave: false,
      // Replay masks all typed input.
      session_recording: {
        // 0-1. Every session, since 2026-09-30: at this traffic level replay
        // earns its keep through individual sessions watched end-to-end, and
        // the 0.25 sample recorded one or two of the week's seven signups and
        // missed the one that mattered. Drop it back when volume justifies.
        // Set here rather than in the PostHog project settings because a
        // client-side value takes precedence over the remote config: this
        // literal is the rate, and moving the dashboard slider will not
        // change it.
        sampleRate: 1,
        maskAllInputs: true,
        blockSelector: "[data-private]",
        maskCapturedNetworkRequestFn: (request) => ({
          ...request,
          name: request.name.split(/[?#]/, 1)[0],
          requestHeaders: undefined,
          requestBody: undefined,
          responseHeaders: undefined,
          responseBody: undefined,
        }),
      },
      before_send: (event) => {
        if (!event) return null;
        // The SDK adds entry URLs to event props and initial URLs to person
        // props ($set_once, including on $identify). Cover both wire shapes.
        for (const candidate of [event.properties, event.$set, event.$set_once,
          event.properties.$set, event.properties.$set_once]) {
          if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) continue;
          const fields = candidate as Record<string, unknown>;
          for (const key of POSTHOG_URL_KEYS) {
            const value = fields[key];
            if (typeof value === "string") fields[key] = withoutChatQuery(value);
          }
        }
        // Replay batches carry their own rrweb URLs. Top-level property
        // redaction does not touch Meta (4) or URL custom (5) frames.
        const snapshots = event.properties.$snapshot_data;
        if (event.event === "$snapshot" && Array.isArray(snapshots)) {
          for (const item of snapshots) {
            if (!item || typeof item !== "object") continue;
            const frame = item as {
              type?: number;
              data?: { href?: string; tag?: string; payload?: { href?: string } };
            };
            if (frame.type === 4 && typeof frame.data?.href === "string") {
              frame.data.href = withoutChatQuery(frame.data.href);
            }
            if (frame.type === 5 && (frame.data?.tag === "$url_changed" || frame.data?.tag === "$pageview")
              && typeof frame.data.payload?.href === "string") {
              frame.data.payload.href = withoutChatQuery(frame.data.payload.href);
            }
          }
        }
        return event;
      },
    });
    // `environment` is a super property because the dashboard's queries filter on it. Must come after
    // init(): register() no-ops until the SDK has loaded.
    register({ environment: currentEnvironment() });
  }, []);
  return null;
}

function PostHogIdentify() {
  const { isSignedIn, userId } = useAuth();
  const { user } = useUser();
  const identifiedRef = useRef<string | null>(null);

  useEffect(() => {
    if (!POSTHOG_KEY) return;

    if (isSignedIn && userId && identifiedRef.current !== userId) {
      // The dashboard reads the person's `email`; the same call sends `$initial_utm_source`.
      identify(userId, { email: user?.primaryEmailAddress?.emailAddress });
      identifiedRef.current = userId;
    } else if (!isSignedIn && identifiedRef.current) {
      reset();
      identifiedRef.current = null;
      // reset() clears super properties.
      register({ environment: currentEnvironment() });
    }
  }, [isSignedIn, userId, user]);

  return null;
}

/**
 * Passive forwarder for uncaught client-side errors and unhandled promise
 * rejections. Does NOT replace the Next.js error page / React error
 * boundary — it just makes sure we see the exception in PostHog so we
 * don't rely on users sending us screenshots. `captureException` in
 * @/lib/analytics already no-ops when PostHog isn't initialised, so this
 * is safe even without NEXT_PUBLIC_POSTHOG_KEY.
 */
function PostHogErrorForwarder() {
  useEffect(() => {
    if (typeof window === "undefined") return;

    const onError = (event: ErrorEvent) => {
      captureException(event.error ?? new Error(event.message));
    };
    const onUnhandledRejection = (event: PromiseRejectionEvent) => {
      captureException(event.reason ?? new Error("unhandledrejection"));
    };

    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onUnhandledRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onUnhandledRejection);
    };
  }, []);

  return null;
}

export function PostHogProvider({ children }: { children: React.ReactNode }) {
  if (!POSTHOG_KEY) {
    return <>{children}</>;
  }

  return (
    <AnalyticsProvider client={posthogClient}>
      <PostHogInit />
      <PostHogIdentify />
      <PostHogErrorForwarder />
      {children}
    </AnalyticsProvider>
  );
}
