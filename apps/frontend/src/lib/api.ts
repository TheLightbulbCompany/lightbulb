import { useCallback, useMemo } from "react";
import { useAuth } from "@clerk/nextjs";

// Use environment variable for production, fallback to localhost for development
export const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

/**
 * Derive the WebSocket gateway URL from `apiUrl`.
 *
 * Hostname rewrite:
 *   api.isol8.co       → ws.isol8.co
 *   api-dev.isol8.co   → ws-dev.isol8.co
 *   api-staging.isol8.co → ws-staging.isol8.co
 *   localhost:8000     → localhost:8000   (untouched — local dev hits the same host)
 *
 * Protocol: http → ws, https → wss. Path `/api/v1` is stripped so callers get
 * the bare WebSocket origin (they append their own path, e.g. `/control-ui/`).
 *
 * An explicit `NEXT_PUBLIC_WS_URL` env var takes precedence over derivation so
 * bespoke environments (e.g. a PR preview against a non-standard gateway) can
 * override without code changes.
 */
export function deriveWebSocketUrl(apiUrl: string): string {
  if (process.env.NEXT_PUBLIC_WS_URL) {
    return process.env.NEXT_PUBLIC_WS_URL;
  }
  return apiUrl
    .replace(/^https:\/\//, "wss://")
    .replace(/^http:\/\//, "ws://")
    // Rewrite the HOSTNAME only. `\/\/` anchors us right after the scheme so a
    // path segment like `/api/v1` can't accidentally match; lookahead `(?=[-.])`
    // requires `-` or `.` after `api` so "apiary.com" wouldn't match and
    // localhost (which doesn't start with "api") is left alone.
    .replace(/\/\/api(?=[-.])/, "//ws")
    .replace(/\/api\/v1$/, "");
}

export const WS_URL = deriveWebSocketUrl(BACKEND_URL);

/**
 * Thrown by `useApi` helpers when the backend returns a non-2xx response.
 *
 * `body` is the parsed JSON response body when Content-Type is JSON, else
 * null. Callers can switch on `status` and `body` to handle structured
 * error payloads (e.g. the `blocked` field from /container/* endpoints).
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message?: string,
  ) {
    super(message ?? `API ${status}`);
    this.name = "ApiError";
  }
}

/**
 * Like useApi.get, but exposes the HTTP status code on success.
 *
 * Use this when a 2xx response code carries semantic meaning that
 * the body alone doesn't capture — e.g. /container/status returning
 * 200 (container info) vs 202 (team workspace pending). The standard
 * useApi.get path returns the body for any 2xx status, swallowing
 * the status code.
 *
 * Returns { status, body }. Throws ApiError on non-2xx, matching
 * useApi's error shape (including the body.detail → message extraction).
 */
export async function fetchJsonWithStatus(
  url: string,
  getToken: () => Promise<string | null>,
): Promise<{ status: number; body: unknown }> {
  const token = await getToken();
  if (!token) {
    throw new ApiError(401, null, "Not authenticated");
  }
  const response = await fetch(`${BACKEND_URL}${url}`, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });

  let body: unknown = null;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      body = await response.json();
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    // Mirror useApi.authenticatedFetch: extract body.detail as a string
    // for ApiError.message so callers reading .message see a useful
    // backend message instead of "API <status>".
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : null;
    const message = typeof detail === "string" ? detail : undefined;
    throw new ApiError(response.status, body, message);
  }

  return { status: response.status, body };
}

interface UploadedFile {
  filename: string;
  path: string;
  size: number;
}

interface UploadResponse {
  uploaded: UploadedFile[];
}

interface ApiMethods {
  syncUser: () => Promise<unknown>;
  get: (endpoint: string) => Promise<unknown>;
  post: (endpoint: string, body: unknown) => Promise<unknown>;
  put: (endpoint: string, body: unknown) => Promise<unknown>;
  patch: (endpoint: string, body: unknown) => Promise<unknown>;
  del: (endpoint: string) => Promise<unknown>;
  patchConfig: (patch: Record<string, unknown>) => Promise<{ status: string; owner_id: string }>;
  uploadFiles: (files: File[], agentId: string) => Promise<UploadResponse>;
  saveWorkspaceFile: (agentId: string, path: string, content: string, tab: "workspace" | "config") => Promise<{ status: string; path: string }>;
}

export function useApi(): ApiMethods {
  const { getToken } = useAuth();

  const authenticatedFetch = useCallback(
    async function (endpoint: string, options: RequestInit = {}): Promise<unknown> {
      const token = await getToken();

      if (!token) {
        throw new Error("No authentication token available");
      }

      // Let the browser set the multipart Content-Type (with boundary)
      // when the body is FormData. Otherwise default to JSON, which is
      // the calling convention for every other method on this hook.
      const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
      const headers: HeadersInit = {
        ...options.headers,
        Authorization: `Bearer ${token}`,
        ...(isFormData ? {} : { "Content-Type": "application/json" }),
      };

      const response = await fetch(`${BACKEND_URL}${endpoint}`, {
        ...options,
        headers,
      });

      if (!response.ok) {
        // Preserve the parsed body on non-2xx so callers can switch on
        // structured error fields (e.g. `blocked` from /container/*
        // endpoints). Falls back to `null` on empty / non-JSON / malformed
        // bodies.
        const ct = response.headers.get("Content-Type") ?? "";
        let body: unknown = null;
        if (ct.includes("application/json")) {
          try {
            body = await response.json();
          } catch {
            // empty/malformed JSON — leave body as null
          }
        }
        const detail =
          body && typeof body === "object" && "detail" in body
            ? (body as { detail?: unknown }).detail
            : undefined;
        const message = typeof detail === "string" ? detail : undefined;
        throw new ApiError(response.status, body, message);
      }

      return response.json();
    },
    [getToken]
  );

  return useMemo(
    () => ({
      syncUser(): Promise<unknown> {
        return authenticatedFetch("/users/sync", { method: "POST" });
      },
      get(endpoint: string): Promise<unknown> {
        return authenticatedFetch(endpoint, { method: "GET" });
      },
      post(endpoint: string, body: unknown): Promise<unknown> {
        return authenticatedFetch(endpoint, {
          method: "POST",
          body: JSON.stringify(body),
        });
      },
      put(endpoint: string, body: unknown): Promise<unknown> {
        return authenticatedFetch(endpoint, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      },
      patch(endpoint: string, body: unknown): Promise<unknown> {
        return authenticatedFetch(endpoint, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
      },
      del(endpoint: string): Promise<unknown> {
        return authenticatedFetch(endpoint, { method: "DELETE" });
      },
      patchConfig(patch: Record<string, unknown>): Promise<{ status: string; owner_id: string }> {
        return authenticatedFetch("/config", {
          method: "PATCH",
          body: JSON.stringify({ patch }),
        }) as Promise<{ status: string; owner_id: string }>;
      },
      uploadFiles(files: File[], agentId: string): Promise<UploadResponse> {
        const formData = new FormData();
        for (const file of files) {
          formData.append("files", file);
        }
        return authenticatedFetch(
          `/container/files?agent_id=${encodeURIComponent(agentId)}`,
          { method: "POST", body: formData },
        ) as Promise<UploadResponse>;
      },
      saveWorkspaceFile(
        agentId: string,
        path: string,
        content: string,
        tab: "workspace" | "config",
      ): Promise<{ status: string; path: string }> {
        return authenticatedFetch(`/container/workspace/${encodeURIComponent(agentId)}/file`, {
          method: "PUT",
          body: JSON.stringify({ path, content, tab }),
        }) as Promise<{ status: string; path: string }>;
      },
    }),
    [authenticatedFetch]
  );
}
