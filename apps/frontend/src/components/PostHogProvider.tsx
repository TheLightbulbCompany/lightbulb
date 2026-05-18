"use client";

import { Suspense, useEffect, useRef } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useAuth, useUser } from "@clerk/nextjs";

import {
  Provider as AnalyticsProvider,
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
      // CEO observability: enable session replay. Record typed text by
      // default so admins can actually see what users tried to do, but
      // mask anything marked sensitive (password fields always; add
      // `data-private` to any DOM node that must not be recorded).
      // See https://posthog.com/docs/session-replay/configuration.
      session_recording: {
        maskAllInputs: false,
        maskInputOptions: {
          password: true,
          email: false,
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

  useEffect(() => {
    if (!POSTHOG_KEY) return;
    const url = pathname + (searchParams?.toString() ? `?${searchParams.toString()}` : "");
    pageview(url);
  }, [pathname, searchParams]);

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
