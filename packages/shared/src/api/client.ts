import { ApiError } from "./error";

/** Backend correlation header, exposed cross-origin via CORS expose_headers. */
export const REQUEST_ID_HEADER = "X-Request-ID";

/**
 * Configuration for the framework-neutral API client.
 *
 * Extracted verbatim from the web frontend's `lib/api.ts` (Phase 1 of the
 * mobile-app plan): the platform wrapper injects the Clerk token getter, the
 * env-derived base URL, and its request-id sink; everything below is shared
 * behavior and must not fork between web and mobile.
 */
export interface ApiClientConfig {
  /** Backend REST base URL including /api/v1. */
  apiUrl: string;
  /** Fresh Clerk bearer token, or null when unauthenticated. */
  getToken: () => Promise<string | null>;
  /**
   * Called with the response's X-Request-ID (null when absent) on every
   * response, success or failure. Web routes this to its support-ticket
   * ring buffer; platforms without a sink omit it.
   */
  onRequestId?: (id: string | null) => void;
}

/**
 * Per-call opt-ins for the Teams BFF cookie flow.
 *
 * These default to "off" so the vast majority of callers (which authenticate
 * purely via the Clerk bearer token) keep their existing behavior. Only the
 * Teams data layer threads these through.
 *
 * - `credentials: "include"` makes the browser attach the HttpOnly Paperclip
 *   session cookie (scoped to `.isol8.co`) on the cross-subdomain request to
 *   the api host. The backend CORS already allows credentials for these
 *   origins. We scope this to Teams calls only (decision D2 — least surface).
 * - `retryOn401` retries the *same* request exactly once on a 401. The backend
 *   reseeds a fresh cookie on the 401 it returns for an expired/rotated
 *   session (body `{"detail":"session refreshed, retry"}`), so the single
 *   retry carries the new cookie and succeeds.
 */
export interface ApiCallOptions {
  credentials?: RequestCredentials;
  retryOn401?: boolean;
  /**
   * Aborts the underlying fetch when it fires. Plumbed straight through to
   * the `fetch()` call below; the caller owns the `AbortController`/timer
   * (see `createMessagingClient`'s `sendMessage`, which times a hung POST
   * out rather than let it hang forever).
   */
  signal?: AbortSignal;
}

export interface ApiMethods {
  syncUser: () => Promise<unknown>;
  get: (endpoint: string, opts?: ApiCallOptions) => Promise<unknown>;
  post: (endpoint: string, body: unknown, opts?: ApiCallOptions) => Promise<unknown>;
  put: (endpoint: string, body: unknown, opts?: ApiCallOptions) => Promise<unknown>;
  patch: (endpoint: string, body: unknown, opts?: ApiCallOptions) => Promise<unknown>;
  del: (endpoint: string, opts?: ApiCallOptions, body?: unknown) => Promise<unknown>;
  patchConfig: (patch: Record<string, unknown>) => Promise<{ status: string; owner_id: string }>;
  /**
   * POST a `multipart/form-data` body. The caller builds the `FormData` (so it
   * can pick the exact field names a given endpoint expects); the client
   * lets the runtime set the multipart `Content-Type` with its boundary.
   */
  upload: (endpoint: string, formData: FormData, opts?: ApiCallOptions) => Promise<unknown>;
  /**
   * GET a binary body as a `Blob`. Needed for authenticated downloads — a plain
   * `<a href>` can't carry the Clerk bearer header, so the bytes are fetched here
   * and handed to the caller (which makes an object URL to trigger the download).
   */
  downloadBlob: (endpoint: string, opts?: ApiCallOptions) => Promise<Blob>;
  saveWorkspaceFile: (agentId: string, path: string, content: string, tab: "workspace" | "config") => Promise<{ status: string; path: string }>;
}

function extractDetailMessage(body: unknown): string | undefined {
  const detail =
    body && typeof body === "object" && "detail" in body
      ? (body as { detail?: unknown }).detail
      : undefined;
  if (typeof detail === "string") return detail;
  // Structured gate bodies ({blocked, code, message} — the Teams trial 402s)
  // carry their human copy on `message`. Without promoting it, ApiError falls
  // back to "API <status>" and every toast shows that instead of the copy the
  // backend wrote for exactly this moment.
  const detailMessage =
    detail && typeof detail === "object" && "message" in detail
      ? (detail as { message?: unknown }).message
      : undefined;
  if (typeof detailMessage === "string" && detailMessage) return detailMessage;
  // Messaging/runtime ApiProblem responses (leave_room, archive, steer, ...)
  // carry no `detail` wrapper at all — the body IS {code, message, retryable,
  // ...} at the top level (core/services/messaging.py ApiProblem.body()).
  // Without this, e.g. the channel-owner Leave rejection surfaced as the
  // useless "API 400" instead of "The room owner cannot leave...".
  const topLevelMessage =
    body && typeof body === "object" && "message" in body
      ? (body as { message?: unknown }).message
      : undefined;
  return typeof topLevelMessage === "string" && topLevelMessage ? topLevelMessage : undefined;
}

async function parseJsonBody(response: Response): Promise<unknown> {
  const ct = response.headers.get("Content-Type") ?? "";
  let body: unknown = null;
  if (ct.includes("application/json")) {
    try {
      body = await response.json();
    } catch {
      // empty/malformed JSON — leave body as null
    }
  }
  return body;
}

/**
 * Like `get`, but exposes the HTTP status code on success.
 *
 * Use this when a 2xx response code carries semantic meaning that
 * the body alone doesn't capture — e.g. /container/status returning
 * 200 (container info) vs 202 (team workspace pending). The standard
 * get path returns the body for any 2xx status, swallowing the
 * status code.
 *
 * `signal` is optional (spec PR-9, CLIENT-HOOK): `useOnboardingStatus`'s
 * fetcher passes an `AbortController` timeout so a genuinely hung request
 * — the research's actual root cause for a throttled-but-visible tab
 * freezing, not a dropped SWR tick — can't wedge the shared bounded loop
 * forever. Omitted by every other caller, matching today's behavior.
 *
 * Returns { status, body }. Throws ApiError on non-2xx, matching
 * the client's error shape (including the body.detail → message extraction).
 */
export async function fetchJsonWithStatus(
  config: ApiClientConfig,
  url: string,
  signal?: AbortSignal,
): Promise<{ status: number; body: unknown }> {
  const token = await config.getToken();
  if (!token) {
    throw new ApiError(401, null, "Not authenticated");
  }
  const response = await fetch(`${config.apiUrl}${url}`, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    signal,
  });

  const requestId = response.headers.get(REQUEST_ID_HEADER);
  config.onRequestId?.(requestId);

  const body = await parseJsonBody(response);

  if (!response.ok) {
    const message = extractDetailMessage(body);
    throw new ApiError(response.status, body, message, requestId);
  }

  return { status: response.status, body };
}

/**
 * Build the authenticated fetch used by every JSON method.
 *
 * Package-internal: `createApiClient` below is the only caller, and the barrel
 * does not re-export this. It stays a named export so `client.test.ts` can drive
 * the auth/request-id/error paths without going through the method surface.
 */
export function createAuthenticatedFetch(config: ApiClientConfig) {
  return async function authenticatedFetch(
    endpoint: string,
    options: RequestInit = {},
    apiOpts: ApiCallOptions = {},
  ): Promise<unknown> {
    const token = await config.getToken();

    if (!token) {
      throw new Error("No authentication token available");
    }

    // Let the runtime set the multipart Content-Type (with boundary)
    // when the body is FormData. Otherwise default to JSON, which is
    // the calling convention for every other method on the client.
    const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
    const headers: HeadersInit = {
      ...options.headers,
      Authorization: `Bearer ${token}`,
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
    };

    // `_attempt` distinguishes the first try (0) from the single retry (1)
    // so the 401-retry below can never loop: we retry at most once, then
    // surface the error like any other non-2xx.
    const doFetch = async (_attempt: number): Promise<unknown> => {
      const response = await fetch(`${config.apiUrl}${endpoint}`, {
        ...options,
        headers,
        signal: apiOpts.signal,
        // Opt-in only: Teams calls pass `credentials: "include"` so the
        // browser attaches the `.isol8.co` Paperclip cookie. Every other
        // caller omits this and keeps fetch's default (`same-origin`).
        ...(apiOpts.credentials ? { credentials: apiOpts.credentials } : {}),
      });

      const requestId = response.headers.get(REQUEST_ID_HEADER);
      config.onRequestId?.(requestId);

      if (!response.ok) {
        // Retry-once on 401 for opted-in (Teams) calls. The backend reseeds
        // a fresh session cookie on this 401, so re-issuing the identical
        // request now carries the new cookie. Guarded to a single retry via
        // `_attempt` — a second 401 falls through to the ApiError throw.
        if (response.status === 401 && apiOpts.retryOn401 && _attempt === 0) {
          return doFetch(1);
        }

        // Preserve the parsed body on non-2xx so callers can switch on
        // structured error fields (e.g. `blocked` from /container/*
        // endpoints). Falls back to `null` on empty / non-JSON / malformed
        // bodies.
        const body = await parseJsonBody(response);
        const message = extractDetailMessage(body);
        throw new ApiError(response.status, body, message, requestId);
      }

      // A 204 carries no body — parsing it as JSON would reject a call that
      // succeeded (the brain DELETE route is a true 204).
      if (response.status === 204) return null;
      return response.json();
    };

    return doFetch(0);
  };
}

// Authenticated GET that returns the raw bytes as a Blob. Kept separate from
// `authenticatedFetch` because that helper always parses the body as JSON on
// success — a binary download must read `.blob()` instead. Mirrors the
// bearer-header + `credentials`-opt-in posture so Teams downloads carry both
// the Clerk token and the `.isol8.co` Paperclip cookie.
function createBlobFetch(config: ApiClientConfig) {
  return async function blobFetch(
    endpoint: string,
    apiOpts: ApiCallOptions = {},
  ): Promise<Blob> {
    const token = await config.getToken();
    if (!token) {
      throw new Error("No authentication token available");
    }
    const response = await fetch(`${config.apiUrl}${endpoint}`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      ...(apiOpts.credentials ? { credentials: apiOpts.credentials } : {}),
    });
    const requestId = response.headers.get(REQUEST_ID_HEADER);
    config.onRequestId?.(requestId);
    if (!response.ok) {
      const body = await parseJsonBody(response);
      const message = extractDetailMessage(body);
      throw new ApiError(response.status, body, message, requestId);
    }
    return response.blob();
  };
}

/** The full REST method surface shared by the web and mobile clients. */
export function createApiClient(config: ApiClientConfig): ApiMethods {
  const authenticatedFetch = createAuthenticatedFetch(config);
  const blobFetch = createBlobFetch(config);

  return {
    syncUser(): Promise<unknown> {
      return authenticatedFetch("/users/sync", { method: "POST" });
    },
    get(endpoint: string, opts?: ApiCallOptions): Promise<unknown> {
      return authenticatedFetch(endpoint, { method: "GET" }, opts);
    },
    post(endpoint: string, body: unknown, opts?: ApiCallOptions): Promise<unknown> {
      return authenticatedFetch(endpoint, {
        method: "POST",
        body: JSON.stringify(body),
      }, opts);
    },
    put(endpoint: string, body: unknown, opts?: ApiCallOptions): Promise<unknown> {
      return authenticatedFetch(endpoint, {
        method: "PUT",
        body: JSON.stringify(body),
      }, opts);
    },
    patch(endpoint: string, body: unknown, opts?: ApiCallOptions): Promise<unknown> {
      return authenticatedFetch(endpoint, {
        method: "PATCH",
        body: JSON.stringify(body),
      }, opts);
    },
    del(endpoint: string, opts?: ApiCallOptions, body?: unknown): Promise<unknown> {
      return authenticatedFetch(
        endpoint,
        body === undefined ? { method: "DELETE" } : { method: "DELETE", body: JSON.stringify(body) },
        opts,
      );
    },
    patchConfig(patch: Record<string, unknown>): Promise<{ status: string; owner_id: string }> {
      return authenticatedFetch("/config", {
        method: "PATCH",
        body: JSON.stringify({ patch }),
      }) as Promise<{ status: string; owner_id: string }>;
    },
    upload(endpoint: string, formData: FormData, opts?: ApiCallOptions): Promise<unknown> {
      return authenticatedFetch(endpoint, { method: "POST", body: formData }, opts);
    },
    downloadBlob(endpoint: string, opts?: ApiCallOptions): Promise<Blob> {
      return blobFetch(endpoint, opts);
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
  };
}
