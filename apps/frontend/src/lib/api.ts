import { useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import {
  ApiError,
  createApiClient,
  fetchJsonWithStatus as fetchJsonWithStatusShared,
  type ApiCallOptions,
  type ApiMethods,
} from "@lightbulb/shared";
import { recordRequestId } from "./requestIdLog";

// The client implementation lives in @lightbulb/shared (extracted so the mobile
// app could share it). This module is the web platform wrapper: it binds the
// Next.js env source, the Clerk hook, and the request-id ring buffer, and
// preserves the historical export surface so feature code never imports the
// shared package directly.

// Use environment variable for production, fallback to localhost for development
export const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

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
