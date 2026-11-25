/**
 * `signup_flow_id` — one id that spans the whole acquisition journey:
 * landing CTA click → Clerk sign-up → onboarding steps → the backend's
 * canonical `trial_started`.
 *
 * Why this exists when PostHog already stitches anonymous → identified:
 * that stitch is a single `$identify` at one moment, and it is exactly the
 * thing that breaks in the cohort we most need to measure — an in-app
 * webview (Instagram, Facebook) that clears storage between the OAuth
 * redirect and the return, or a user who signs up on one device after
 * clicking the CTA on another. When the stitch fails the journey silently
 * splits into two unrelated persons and the funnel undercounts with no
 * error anywhere. A flow id carried as an explicit property is a second,
 * independent key to join on.
 *
 * Registered ONCE as a PostHog super property (see PostHogProvider), so
 * every client event carries it with no per-call-site plumbing, and sent
 * to the backend on the `/container/status` poll so the ONE server-side
 * `trial_started` lands on the same id.
 */

const FLOW_ID_KEY = "lightbulb:signup_flow_id:v1";

// Storage-disabled fallback. Safari private mode and several in-app
// webviews throw on localStorage access — the exact browsers this id is
// meant to measure — so a process-lifetime id is better than none: it
// still joins every event within the page session.
let memoryFlowId: string | null = null;

function newId(): string {
  // crypto.randomUUID needs a secure context AND iOS 15.4+; the in-app
  // webviews we care about are not all there yet, so keep a fallback.
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    /* fall through */
  }
  return `f-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/**
 * Start a NEW journey: drop the stored id so the next `signupFlowId()` mints
 * a fresh one. Called on sign-out. Without this, two people signing up on one
 * browser (a shared machine, a QA device, a user re-signing-up after a wipe)
 * would share a single flow id, and joining on it would MERGE two unrelated
 * journeys — strictly worse than the identity-stitch failure the id exists
 * to work around.
 */
export function rotateSignupFlowId(): void {
  memoryFlowId = null;
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(FLOW_ID_KEY);
  } catch {
    /* storage-disabled: memory id was already cleared above */
  }
}

/** The current journey's flow id, minting one on first call. */
export function signupFlowId(): string {
  if (typeof window === "undefined") return "";
  try {
    const existing = window.localStorage.getItem(FLOW_ID_KEY);
    if (existing) return existing;
    const created = newId();
    window.localStorage.setItem(FLOW_ID_KEY, created);
    return created;
  } catch {
    memoryFlowId ??= newId();
    return memoryFlowId;
  }
}

// ---------------------------------------------------------------------------
// Browser context
// ---------------------------------------------------------------------------

/** In-app webviews, in probe order — Instagram's UA also contains
 *  "FBAV"-adjacent markers on some builds, so the more specific app wins. */
const IN_APP_BROWSERS: ReadonlyArray<readonly [string, RegExp]> = [
  ["instagram", /Instagram/i],
  ["facebook", /FBAN|FBAV|FB_IAB/i],
  ["tiktok", /BytedanceWebview|musical_ly|TikTok/i],
  ["linkedin", /LinkedInApp/i],
  ["snapchat", /Snapchat/i],
  ["twitter", /Twitter/i],
  ["line", /\bLine\//i],
  ["wechat", /MicroMessenger/i],
];

export interface BrowserContext {
  /** The app hosting the page: an in-app browser's name, else the engine. */
  browser_context: string;
  is_in_app_browser: boolean;
}

/**
 * PostHog's own `$browser` reports "Mobile Safari" for EVERY iOS in-app
 * webview, because that is literally what the UA claims — which makes the
 * one cohort with a distinct auth-redirect failure mode invisible in the
 * funnel. This names it.
 */
export function browserContext(ua?: string): BrowserContext {
  const agent = ua ?? (typeof navigator === "undefined" ? "" : navigator.userAgent);
  for (const [name, probe] of IN_APP_BROWSERS) {
    if (probe.test(agent)) return { browser_context: name, is_in_app_browser: true };
  }
  // Order matters: Edge and Chrome both claim Safari; Chrome on iOS is CriOS.
  if (/Edg\//.test(agent)) return { browser_context: "edge", is_in_app_browser: false };
  if (/CriOS|Chrome\//.test(agent)) return { browser_context: "chrome", is_in_app_browser: false };
  if (/FxiOS|Firefox\//.test(agent)) return { browser_context: "firefox", is_in_app_browser: false };
  if (/Safari\//.test(agent)) return { browser_context: "safari", is_in_app_browser: false };
  return { browser_context: "other", is_in_app_browser: false };
}
