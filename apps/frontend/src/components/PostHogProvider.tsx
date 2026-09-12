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
  register,
  reset,
} from "@/lib/analytics";
import {
  browserContext,
  currentSignupStep,
  rotateSignupFlowId,
  setSignupStep,
  signupFlowId,
} from "@/lib/signupFlow";

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

/**
 * The PostHog project is shared by dev and prod, so client events carry no
 * environment on their own — a dev session pollutes prod funnels unless
 * every event is tagged. `NEXT_PUBLIC_API_URL` is baked in per-deploy (see
 * CLAUDE.md's "API URL by environment"), so its host is the one build-time
 * signal for which environment this bundle is running in.
 */
export function resolveEnvironment(apiUrl: string | undefined): "prod" | "dev" {
  try {
    return new URL(apiUrl ?? "").hostname === "api.flickmeon.ai" ? "prod" : "dev";
  } catch {
    return "dev";
  }
}

const ENVIRONMENT = resolveEnvironment(process.env.NEXT_PUBLIC_API_URL);

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
    // Super properties: merged by PostHog into every subsequent event, so
    // `signup_flow_id` reaches call sites that know nothing about it, and
    // the browser context names in-app webviews that `$browser` reports
    // indistinguishably as "Mobile Safari". Must come after init() —
    // register() no-ops until the SDK has loaded.
    register({ signup_flow_id: signupFlowId(), environment: ENVIRONMENT, ...browserContext() });
  }, []);
  return null;
}

/**
 * `signup_exit` — the user left while still inside the onboarding flow,
 * tagged with the step they left FROM. This is the event that turns
 * "people drop off somewhere" into "people drop off HERE".
 *
 * It is NOT emitted from /sign-up or /sign-in, on purpose. A `pagehide`
 * there is ambiguous by construction: "Continue with Google/Apple" is a
 * cross-origin full-document navigation that fires `pagehide` for every
 * SUCCESSFUL OAuth signup, and there is no way to tell it from an abandon at
 * the moment it happens. Emitting there would count every OAuth success as
 * a drop-off — in exactly the in-app-webview cohort this exists to measure.
 * The sign-up page's own drop-off is `reached /sign-up AND never
 * $identify`, which needs no event and cannot be fooled by a redirect;
 * autocapture (on for every route but /chat) already records the OAuth
 * button click itself.
 *
 * `pagehide`, not `beforeunload`: mobile Safari skips `beforeunload`
 * entirely when a page enters the back/forward cache, which is the normal
 * way an iOS user leaves a tab. `send_instantly` bypasses PostHog's batch
 * queue so the request is at least issued before the document goes away
 * (posthog-js ignores a `transport` option on capture — its own pageleave
 * path is the only thing that gets sendBeacon; for a custom event the
 * keepalive fetch this produces is the best available).
 *
 * Fires at most once per step per page session: a bfcache restore/freeze
 * cycle can dispatch `pagehide` repeatedly from the same step, and a
 * drop-off count that double-counts one user is worse than useless.
 */
function SignupExitTracker() {
  const firedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const onHide = () => {
      const step = currentSignupStep();
      if (!step || firedRef.current.has(step)) return;
      firedRef.current.add(step);
      capture(
        "signup_exit",
        { step, route: window.location.pathname },
        { send_instantly: true },
      );
    };
    window.addEventListener("pagehide", onHide);
    return () => window.removeEventListener("pagehide", onHide);
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
  // capture in the block — pageview, landing_viewed, the checkout return —
  // so a StrictMode dev double-invoke cannot double any of them.
  const lastSentUrlRef = useRef<string | null>(null);

  // Per-URL effect, keyed on the URL STRING. Invariants: one send per
  // navigation; independent of when Clerk resolves `isSignedIn` (that lives
  // in the second effect); immune to searchParams object-identity churn.
  useEffect(() => {
    if (!POSTHOG_KEY) return;
    if (lastSentUrlRef.current !== url && pageview(url)) {
      lastSentUrlRef.current = url;
      if (pathname === "/") {
        capture("landing_viewed", { path: url });
      }
      const checkoutStatus = searchParams.get("checkout") ?? searchParams.get("subscription");
      if (checkoutStatus === "success" || checkoutStatus === "cancel") {
        capture(
          checkoutStatus === "success"
            ? "subscription_checkout_returned_success"
            : "subscription_checkout_canceled",
          { path: pathname, provider_choice: searchParams.get("provider") },
        );
      }
    }

    // Which step `signup_exit` reports. /chat is owned by ProvisioningStepper
    // (it knows the provisioning phase and whether this is a genuine first
    // run). /onboarding is Clerk's team-creation / invitation screen — an
    // expensive, unambiguous place to abandon, so it gets its own step.
    // /sign-up and /sign-in deliberately get NONE: see SignupExitTracker for
    // why an exit there cannot be told from an OAuth redirect. Every other
    // route clears it so a later exit reports nothing.
    if (pathname?.startsWith("/onboarding")) {
      setSignupStep("team_setup");
    } else if (!pathname?.startsWith("/chat")) {
      setSignupStep(null);
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
      register({ signup_flow_id: signupFlowId(), environment: ENVIRONMENT, ...browserContext() });
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
      <SignupExitTracker />
      <PostHogErrorForwarder />
      {children}
    </AnalyticsProvider>
  );
}
