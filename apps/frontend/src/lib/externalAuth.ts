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

/** Open `url` in the system browser. Tauri only — callers gate on isTauri(). */
export function openExternalUrl(url: string): void {
  void (window as any).__TAURI__.core.invoke("plugin:shell|open", { path: url });
}

/**
 * Hand the user to an external authorize/onboarding URL.
 *
 * Returns true when the URL opened in the system browser (Tauri) — the caller
 * stays mounted and should poll its status endpoint for completion. Returns
 * false on the web, where the same-window navigation discards the React tree.
 */
export function openAuthUrl(url: string): boolean {
  if (isTauri()) {
    openExternalUrl(url);
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
    openExternalUrl(url);
  }
}

/**
 * Poll `check` every `intervalMs` until it returns true or `timeoutMs`
 * elapses. Resolves true iff the check passed; check errors are treated as
 * "not yet" (transient status-fetch failures must not end the wait).
 */
// ponytail: bounded poll, no cancel affordance — the wait self-clears at the
// timeout; add an AbortSignal if a flow ever needs a Dismiss button.
export async function pollUntil(
  check: () => Promise<boolean>,
  {
    intervalMs = 3000,
    timeoutMs = 180_000,
  }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      if (await check()) return true;
    } catch {
      // transient failure — keep polling
    }
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
