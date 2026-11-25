"use client";

import { Suspense, useEffect, useRef } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useAuth, useUser } from "@clerk/nextjs";

import {
  Provider as AnalyticsProvider,
  capture,
  captureException,
  client as posthogClient,
  identify,
  init as initAnalytics,
  isMarketingRoute,
  pageview,
  register,
  reset,
  withoutChatQuery,
} from "@/lib/analytics";
import { browserContext, rotateSignupFlowId, signupFlowId } from "@/lib/signupFlow";

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
const SIGNUP_INTENT_KEY = "isol8:signup_intent:v1";
const SIGNUP_STARTED_IDENTIFIED_PREFIX = "isol8:signup_started_identified:";

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

function safeStorageGet(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

const LANDING_SESSION_KEY = "lightbulb:landing_viewed";

/** True when the flag was already set this browser session; sets it otherwise. */
function safeSessionFlag(key: string): boolean {
  if (typeof window === "undefined") return true;
  try {
    if (window.sessionStorage.getItem(key)) return true;
    window.sessionStorage.setItem(key, "1");
    return false;
  } catch {
    return false; // storage off: count every entry rather than none
  }
}

function safeStorageSet(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Signup stitching is analytics-only; storage-disabled browsers still work.
  }
}

function safeStorageRemove(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Analytics-only cleanup.
  }
}

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
      capture_pageleave: true,
      // Autocapture stays on (the default). Replay masks all typed input.
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
    // Super properties: merged by PostHog into every subsequent event, so
    // `signup_flow_id` reaches call sites that know nothing about it, and
    // the browser context names in-app webviews that `$browser` reports
    // indistinguishably as "Mobile Safari". Must come after init() —
    // register() no-ops until the SDK has loaded.
    register({ signup_flow_id: signupFlowId(), environment: currentEnvironment(), ...browserContext() });
  }, []);
  return null;
}

function PostHogPageview() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { isSignedIn } = useAuth();
  const queryString = searchParams?.toString() ?? "";
  const url = pathname + (queryString ? `?${queryString}` : "");
  // The URL whose per-URL events were last SENT. Set only after a real send
  // (pageview() reports whether posthog was loaded). One ref guards every
  // capture in the block — pageview, landing_viewed —
  // so a StrictMode dev double-invoke cannot double any of them.
  const lastSentUrlRef = useRef<string | null>(null);

  // Per-URL effect, keyed on the URL STRING. Invariants: one send per
  // navigation; independent of when Clerk resolves `isSignedIn` (that lives
  // in the second effect); immune to searchParams object-identity churn.
  useEffect(() => {
    if (!POSTHOG_KEY) return;
    if (lastSentUrlRef.current !== url && pageview(url)) {
      lastSentUrlRef.current = url;
      // The visit's entry on the website: the first marketing page of the browser session,
      // whichever page that is (T23; it used to be "/" only, and every time).
      if (isMarketingRoute(pathname) && !safeSessionFlag(LANDING_SESSION_KEY)) {
        capture("landing_viewed", { path: url });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `url` is derived from pathname + searchParams; keying on the string is the point.
  }, [url]);

  // Sign-up intent stash — the one thing that needs Clerk's answer.
  // Invariant: stash ONLY when Clerk has resolved to signed-out
  // (`=== false`); `undefined` is "not resolved yet", and a signed-in user
  // opening /sign-up must never leave an intent for PostHogIdentify to replay
  // as a signup.
  useEffect(() => {
    if (!POSTHOG_KEY) return;
    if (isSignedIn === false && pathname?.startsWith("/sign-up")) {
      safeStorageSet(SIGNUP_INTENT_KEY, JSON.stringify({
        path: url,
        provider_choice: searchParams.get("provider"),
        timestamp: new Date().toISOString(),
      }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see above.
  }, [url, isSignedIn]);

  return null;
}

function PostHogIdentify() {
  const { isSignedIn, userId } = useAuth();
  const { user } = useUser();
  const identifiedRef = useRef<string | null>(null);

  useEffect(() => {
    if (!POSTHOG_KEY) return;

    if (isSignedIn && userId && identifiedRef.current !== userId) {
      identify(userId, {
        email: user?.primaryEmailAddress?.emailAddress,
        firstName: user?.firstName,
        lastName: user?.lastName,
        // Also a PERSON property, not just a super property: it is the
        // fallback join key for server-side events (trial_started) if the
        // `?flow=` hand-off on the status poll never lands.
        signup_flow_id: signupFlowId(),
      });
      identifiedRef.current = userId;

      const stitchedKey = `${SIGNUP_STARTED_IDENTIFIED_PREFIX}${userId}`;
      const pendingSignupIntent = safeStorageGet(SIGNUP_INTENT_KEY);
      if (pendingSignupIntent && safeStorageGet(stitchedKey) !== "1") {
        let pending: Record<string, unknown> = {};
        try {
          pending = JSON.parse(pendingSignupIntent) as Record<string, unknown>;
        } catch {
          pending = {};
        }
        capture("signup_started", {
          source: "clerk_signup_completed",
          identity_stitched: true,
          signup_path: pending.path,
          provider_choice: pending.provider_choice,
          signup_intent_timestamp: pending.timestamp,
        });
        safeStorageSet(stitchedKey, "1");
        safeStorageRemove(SIGNUP_INTENT_KEY);
      }
    } else if (!isSignedIn && identifiedRef.current) {
      reset();
      identifiedRef.current = null;
      // A sign-out ends the journey. Mint a NEW flow id before re-registering
      // — reset() clears super properties, and the next person to sign up on
      // this browser must not inherit the previous journey's id.
      rotateSignupFlowId();
      register({ signup_flow_id: signupFlowId(), environment: currentEnvironment(), ...browserContext() });
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
      {/* Suspense required: PostHogPageview reads useSearchParams(), which
          forces the whole subtree out of static prerendering unless isolated
          in a Suspense boundary. Without this, pages like /_not-found fail
          prerender with the CSR-bailout error. PostHogPageview renders null
          so fallback={null} is a true no-op. */}
      <Suspense fallback={null}>
        <PostHogPageview />
      </Suspense>
      <PostHogIdentify />
      <PostHogErrorForwarder />
      {children}
    </AnalyticsProvider>
  );
}
