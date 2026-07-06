import { useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import {
  ApiError,
  createApiClient,
  deriveWebSocketUrl as deriveWebSocketUrlShared,
  fetchJsonWithStatus as fetchJsonWithStatusShared,
  type ApiCallOptions,
  type ApiMethods,
} from "@isol8/shared";
import { recordRequestId } from "./requestIdLog";

// The client implementation lives in @isol8/shared (extracted for the mobile
// app — see docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md). This
// module is the web platform wrapper: it binds the Next.js env source, the
// Clerk hook, and the request-id ring buffer, and preserves the historical
// export surface so feature code never imports the shared package directly.

// Use environment variable for production, fallback to localhost for development
export const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

/**
 * Derive the WebSocket gateway URL from `apiUrl` — see the shared
 * implementation for the rewrite rules. `NEXT_PUBLIC_WS_URL` takes precedence
 * over derivation so bespoke environments (e.g. a PR preview against a
 * non-standard gateway) can override without code changes; read at call time
 * so tests can stub it per-case.
 */
export function deriveWebSocketUrl(apiUrl: string): string {
  return deriveWebSocketUrlShared(apiUrl, process.env.NEXT_PUBLIC_WS_URL);
}

export const WS_URL = deriveWebSocketUrl(BACKEND_URL);

export { ApiError };
export type { ApiCallOptions };

/**
 * Like useApi.get, but exposes the HTTP status code on success — e.g.
 * /container/status returning 200 (container info) vs 202 (team workspace
 * pending). Throws ApiError on non-2xx, matching useApi's error shape.
 */
export async function fetchJsonWithStatus(
  url: string,
  getToken: () => Promise<string | null>,
): Promise<{ status: number; body: unknown }> {
  return fetchJsonWithStatusShared(
    { apiUrl: BACKEND_URL, getToken, onRequestId: recordRequestId },
    url,
  );
}

export function useApi(): ApiMethods {
  const { getToken } = useAuth();

  return useMemo(
    () =>
      createApiClient({
        apiUrl: BACKEND_URL,
        getToken,
        onRequestId: recordRequestId,
      }),
    [getToken],
  );
}
