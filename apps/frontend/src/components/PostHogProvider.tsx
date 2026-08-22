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
  pageview,
  reset,
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
const SIGNUP_INTENT_KEY = "isol8:signup_intent:v1";
const SIGNUP_STARTED_IDENTIFIED_PREFIX = "isol8:signup_started_identified:";

function safeStorageGet(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
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
      // ON everywhere EXCEPT /chat.
      //
      // The hand-instrumented events in @/lib/analytics cover the funnel we
      // knew to measure. Launch is the case they cannot cover: you cannot
      // pre-instrument a drop-off nobody has seen yet, and first real traffic
      // is exactly when an uninstrumented dead end is worth more than a clean
      // event taxonomy. This is the safety net for that window — revisit once
      // the funnel has been read against real users.
      //
      // The /chat exclusion is the load-bearing part, not a detail.
      // Autocapture serialises the text of the clicked element and its
      // ancestors into `$element_chain`. On marketing and onboarding pages
      // that text is our own copy — harmless. On /chat the DOM IS the
      // customer's business conversation, so a click on a copy button or a
      // link inside an agent response would carry conversation content into
      // analytics props, bypassing the no-PII-in-props discipline held
      // everywhere else. That is customer data somewhere it was never meant
      // to go, on a product sold on confidentiality.
      //
      // The original perf objection lives on the same page: a DOM-path walk
      // per interaction on the one main thread already saturated by an
      // un-virtualized streaming transcript. Excluding /chat answers both.
      //
      // Heatmaps are a separate subsystem (`capture_heatmaps`), unaffected.
      autocapture: {
        url_ignorelist: [/\/chat/],
      },
      // CEO observability: keep session replay, but sample it instead of
      // recording every session. rrweb installs a document-wide
      // MutationObserver and serialises every mutation; on /chat the
      // transcript is un-virtualized and mutates continuously while a
      // response streams, so recording is sustained main-thread work rather
      // than a one-off cost. Sampling is decided once per session and is
      // deterministic on the session id — an unsampled session never starts
      // rrweb at all, so the majority now pay nothing.
      //
      // Recording typed text stays on so admins can still see what users
      // tried to do, and every masking rule below is unchanged (password +
      // email fields always; add `data-private` to any DOM node that must
      // not be recorded). Email is masked because replay records typed input
      // verbatim including on the Clerk sign-in/sign-up screens — a PII
      // channel that bypasses the no-PII-in-props discipline everywhere else.
      // See https://posthog.com/docs/session-replay/how-to-control-which-sessions-you-record.
      session_recording: {
        // 0-1; ~1 in 4 sessions. Kept deliberately high because at this
        // traffic level replay earns its keep through individual sessions
        // watched end-to-end, not through aggregate coverage — a token 1-5%
        // would leave too few real sessions to learn anything from. Set here
        // rather than in the PostHog project settings because a client-side
        // value takes precedence over the remote config: this literal is the
        // rate, and moving the dashboard slider will not change it.
        sampleRate: 0.25,
        maskAllInputs: false,
        maskInputOptions: {
          password: true,
          email: true,
        },
        blockSelector: "[data-private]",
      },
      // Explicit — PostHog's project-level dashboard has a master switch
      // too, but being explicit here means local dev + preview branches
      // behave the same as prod.
      disable_session_recording: false,
    });
  }, []);
  return null;
}

function PostHogPageview() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { isSignedIn } = useAuth();
  const lastCheckoutEventRef = useRef<string | null>(null);

  useEffect(() => {
    if (!POSTHOG_KEY) return;
    const queryString = searchParams?.toString() ?? "";
    const url = pathname + (queryString ? `?${queryString}` : "");
    pageview(url);
    if (pathname === "/") {
      capture("landing_viewed", { path: url });
    }

    if (!isSignedIn && pathname?.startsWith("/sign-up")) {
      safeStorageSet(SIGNUP_INTENT_KEY, JSON.stringify({
        path: url,
        provider_choice: searchParams.get("provider"),
        timestamp: new Date().toISOString(),
      }));
    }

    const checkoutStatus = searchParams.get("checkout") ?? searchParams.get("subscription");
    if ((checkoutStatus === "success" || checkoutStatus === "cancel") && lastCheckoutEventRef.current !== url) {
      lastCheckoutEventRef.current = url;
      capture(
        checkoutStatus === "success"
          ? "subscription_checkout_returned_success"
          : "subscription_checkout_canceled",
        {
          path: pathname,
          provider_choice: searchParams.get("provider"),
        },
      );
    }
  }, [pathname, searchParams, isSignedIn]);

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
