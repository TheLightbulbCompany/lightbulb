/**
 * External authorize/onboarding URL hand-off.
 */

const FROM_APP_KEY = "lightbulb.fromApp";

/**
 * Latch `?from=app` (the iOS app opening this page in Safari) for the tab, so
 * it survives the SPA rewriting the URL before the user clicks Connect.
 */
function latchFromApp(): void {
  try {
    if (new URLSearchParams(window.location.search).get("from") === "app") {
      sessionStorage.setItem(FROM_APP_KEY, "1");
    }
  } catch {
    // storage blocked: the tab just behaves like plain web
  }
}
if (typeof window !== "undefined") latchFromApp();

/**
 * The OAuth consent will finish in a Safari tab the iOS app opened, so the
 * callback should send the user back to the app (`return_to=ios`), not into
 * the web workspace.
 */
export function returnsToApp(): "ios" | null {
  if (typeof window === "undefined") return null;
  latchFromApp();
  try {
    return sessionStorage.getItem(FROM_APP_KEY) === "1" ? "ios" : null;
  } catch {
    return null;
  }
}

/**
 * Hand the user to an external authorize/onboarding URL: a same-window
 * navigation that discards the React tree; the provider redirects back.
 */
export async function openAuthUrl(url: string): Promise<void> {
  window.location.href = url;
}
