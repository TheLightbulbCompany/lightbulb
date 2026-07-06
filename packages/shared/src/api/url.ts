/**
 * Derive the WebSocket gateway URL from `apiUrl`.
 *
 * Hostname rewrite:
 *   api.isol8.co         → ws.isol8.co
 *   api-dev.isol8.co     → ws-dev.isol8.co
 *   api-staging.isol8.co → ws-staging.isol8.co
 *   localhost:8000       → localhost:8000   (untouched — local dev hits the same host)
 *
 * Protocol: http → ws, https → wss. Path `/api/v1` is stripped so callers get
 * the bare WebSocket origin (they append their own path, e.g. `/control-ui/`).
 *
 * `override` short-circuits derivation so bespoke environments (e.g. a PR
 * preview against a non-standard gateway) can override without code changes.
 * Each platform wrapper injects its own env source (NEXT_PUBLIC_WS_URL /
 * EXPO_PUBLIC_WS_URL) — this module never reads process.env.
 */
export function deriveWebSocketUrl(
  apiUrl: string,
  override?: string | null,
): string {
  if (override) {
    return override;
  }
  return (
    apiUrl
      .replace(/^https:\/\//, "wss://")
      .replace(/^http:\/\//, "ws://")
      // Rewrite the HOSTNAME only. `\/\/` anchors us right after the scheme so a
      // path segment like `/api/v1` can't accidentally match; lookahead `(?=[-.])`
      // requires `-` or `.` after `api` so "apiary.com" wouldn't match and
      // localhost (which doesn't start with "api") is left alone.
      .replace(/\/\/api(?=[-.])/, "//ws")
      .replace(/\/api\/v1$/, "")
  );
}
