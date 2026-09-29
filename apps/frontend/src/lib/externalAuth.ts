/**
 * External authorize/onboarding URL hand-off, desktop-aware.
 *
 * In the Tauri desktop app the webview must never navigate to a provider's
 * authorize page: identity providers block embedded webviews ("sign in with
 * Google" bounces to Safari), the OAuth callback then completes in the system
 * browser's session, and the webview is left stranded on the provider page
 * forever. So under Tauri every external authorize URL opens in the SYSTEM
 * browser via the shell plugin (`shell:allow-open` is already granted to the
 * remote origins in the desktop capabilities), and the in-app surface polls
 * its existing status endpoint until the grant lands. On the web nothing
 * changes: same-window navigation, the provider redirects back into the app.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Running inside the Tauri desktop webview (withGlobalTauri is on). */
export function isTauri(): boolean {
  return (
    typeof window !== "undefined" &&
    Boolean((window as any).__TAURI__?.core?.invoke)
  );
}

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
 * The OAuth consent will finish outside a Lightbulb app — the desktop shell's
 * system browser, or a Safari tab the iOS app opened — so the callback should
 * send the user back to the app (`return_to=app`), not into the web workspace.
 */
export function returnsToApp(): boolean {
  if (isTauri()) return true;
  if (typeof window === "undefined") return false;
  latchFromApp();
  try {
    return sessionStorage.getItem(FROM_APP_KEY) === "1";
  } catch {
    return false;
  }
}

/** User-facing copy when the shell-open invoke itself fails. */
export const BROWSER_OPEN_FAILED_MSG =
  "Couldn't open your browser. Try again.";

/**
 * Open `url` in the system browser. Tauri only — callers gate on isTauri().
 * Rejects with a user-facing Error when the shell plugin refuses, so a caller
 * never proceeds (poll, approve, …) on an open the owner never saw.
 */
export async function openExternalUrl(url: string): Promise<void> {
  try {
    await (window as any).__TAURI__.core.invoke("plugin:shell|open", {
      path: url,
    });
  } catch {
    // Tauri invoke rejects with a plain string — wrap it so callers get an
    // Error with copy fit for notifyApiError/toasts.
    throw new Error(BROWSER_OPEN_FAILED_MSG);
  }
}

/**
 * Hand the user to an external authorize/onboarding URL.
 *
 * Resolves true when the URL opened in the system browser (Tauri) — the
 * caller stays mounted and should poll its status endpoint for completion.
 * Resolves false on the web, where the same-window navigation discards the
 * React tree. Rejects (Tauri only) when the browser could not be opened —
 * callers must surface that instead of entering a waiting state.
 */
export async function openAuthUrl(url: string): Promise<boolean> {
  if (isTauri()) {
    await openExternalUrl(url);
    return true;
  }
  window.location.href = url;
  return false;
}

/**
 * Anchor onClick guard for external links rendered with target="_blank":
 * WKWebView blocks/misroutes popups, so under Tauri open the URL in the
 * system browser instead. No-op on the web (the anchor works as written).
 */
export function externalLinkClick(
  e: { preventDefault(): void },
  url: string,
): void {
  if (isTauri()) {
    e.preventDefault();
    // Anchor context — nowhere structured to surface a failure; the URL is
    // also rendered as text beside these links, so a rare shell failure
    // degrades to "type it yourself".
    void openExternalUrl(url).catch(() => {});
  }
}

/**
 * Poll `check` every `intervalMs` until it returns true, `timeoutMs` elapses,
 * or `signal` aborts. Resolves true iff the check passed; check errors are
 * treated as "not yet" (transient status-fetch failures must not end the
 * wait). Callers pass an unmount-tied AbortSignal so an abandoned wait stops
 * issuing status GETs.
 */
// ponytail: abort is observed at loop edges, so a fetch already in flight
// finishes and cancellation lags by up to one interval — fine at 3s.
export async function pollUntil(
  check: () => Promise<boolean>,
  {
    intervalMs = 3000,
    timeoutMs = 180_000,
    signal,
  }: { intervalMs?: number; timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (signal?.aborted) return false;
    try {
      if (await check()) return true;
    } catch {
      // transient failure — keep polling
    }
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
