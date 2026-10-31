import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, createAuthenticatedFetch, fetchJsonWithStatus } from "./client";
import { ApiError } from "./error";

const CONFIG = {
  apiUrl: "https://api-test.isol8.co/api/v1",
  getToken: async () => "tok_123",
};

function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createAuthenticatedFetch", () => {
  it("attaches the bearer token and JSON content type", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    const authedFetch = createAuthenticatedFetch(CONFIG);

    await authedFetch("/users/me", { method: "GET" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api-test.isol8.co/api/v1/users/me");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok_123");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("throws a plain Error (not ApiError) when unauthenticated", async () => {
    const authedFetch = createAuthenticatedFetch({ ...CONFIG, getToken: async () => null });
    // The distinction is load-bearing: web callers catch ApiError for backend
    // failures but treat the no-token throw as an auth-not-ready signal.
    await expect(authedFetch("/x")).rejects.toThrow("No authentication token available");
    await expect(authedFetch("/x")).rejects.not.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("retries exactly once on 401 when retryOn401 is set", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "session refreshed, retry" }))
      .mockResolvedValueOnce(jsonResponse(200, { fine: true }));
    const authedFetch = createAuthenticatedFetch(CONFIG);

    const result = await authedFetch("/teams/threads", { method: "GET" }, { retryOn401: true, credentials: "include" });

    expect(result).toEqual({ fine: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
  });

  it("does not loop on a second consecutive 401", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "nope" }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "still nope" }));
    const authedFetch = createAuthenticatedFetch(CONFIG);

    await expect(
      authedFetch("/teams/threads", { method: "GET" }, { retryOn401: true }),
    ).rejects.toMatchObject({ status: 401, message: "still nope" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never retries 401 without the opt-in", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "expired" }));
    const authedFetch = createAuthenticatedFetch(CONFIG);

    await expect(authedFetch("/users/me")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("surfaces body.detail as ApiError.message with parsed body and request id", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(402, { detail: "credits required", blocked: { code: "credits_required" } }, { "X-Request-ID": "req_9" }),
    );
    const authedFetch = createAuthenticatedFetch(CONFIG);

    try {
      await authedFetch("/container/status");
      expect.unreachable();
    } catch (e) {
      const err = e as ApiError;
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(402);
      expect(err.message).toBe("credits required");
      expect(err.body).toMatchObject({ blocked: { code: "credits_required" } });
      expect(err.requestId).toBe("req_9");
    }
  });

  it("promotes a structured detail's message to ApiError.message", async () => {
    // The Teams trial gates 402 with {blocked, code, message}. Only the
    // string-detail path was promoted, so these surfaced as "API 402" —
    // truthy, so even notify fallbacks never fired.
    fetchMock.mockResolvedValueOnce(
      jsonResponse(402, {
        detail: { blocked: true, code: "trial_daily_cap", message: "You've used today's free usage." },
      }),
    );
    const authedFetch = createAuthenticatedFetch(CONFIG);

    try {
      await authedFetch("/teams/issues/1/comments");
      expect.unreachable();
    } catch (e) {
      const err = e as ApiError;
      expect(err).toBeInstanceOf(ApiError);
      expect(err.message).toBe("You've used today's free usage.");
      expect(err.body).toMatchObject({ detail: { code: "trial_daily_cap" } });
    }
  });

  it("promotes a top-level ApiProblem message with no detail wrapper", async () => {
    // Messaging/runtime ApiProblem responses (leave_room, archive, steer, ...)
    // have no `detail` key at all -- the body IS {code, message, retryable}
    // at the top level. E.g. a channel owner's Leave rejection.
    fetchMock.mockResolvedValueOnce(jsonResponse(400, {
      code: "INVALID_REQUEST",
      message: "The room owner cannot leave. Make someone else an admin and transfer ownership first.",
      retryable: false,
    }));
    const authedFetch = createAuthenticatedFetch(CONFIG);

    try {
      await authedFetch("/messaging/rooms/r1/leave", { method: "POST" });
      expect.unreachable();
    } catch (e) {
      const err = e as ApiError;
      expect(err.message).toBe("The room owner cannot leave. Make someone else an admin and transfer ownership first.");
    }
  });

  it("falls back to a generic message, not the technical status line, when nothing usable is found", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(422, { detail: [{ loc: ["body"], msg: "invalid" }] }));
    const authedFetch = createAuthenticatedFetch(CONFIG);

    await expect(authedFetch("/x")).rejects.toThrow("Something went wrong");
  });

  it("reports request ids to onRequestId on success and failure", async () => {
    const seen: Array<string | null> = [];
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, {}, { "X-Request-ID": "req_a" }))
      .mockResolvedValueOnce(jsonResponse(500, null, { "X-Request-ID": "req_b" }));
    const authedFetch = createAuthenticatedFetch({ ...CONFIG, onRequestId: (id) => seen.push(id) });

    await authedFetch("/one");
    await authedFetch("/two").catch(() => undefined);

    expect(seen).toEqual(["req_a", "req_b"]);
  });

  it("skips the JSON content type for FormData bodies", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { uploaded: [] }));
    const authedFetch = createAuthenticatedFetch(CONFIG);
    const form = new FormData();

    await authedFetch("/container/files", { method: "POST", body: form });

    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(headers["Content-Type"]).toBeUndefined();
    expect(headers.Authorization).toBe("Bearer tok_123");
  });
});

describe("fetchJsonWithStatus", () => {
  it("returns both status and body for 2xx", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(202, { pending: true }));

    const result = await fetchJsonWithStatus(CONFIG, "/container/status");

    expect(result).toEqual({ status: 202, body: { pending: true } });
  });

  it("throws ApiError(401) with 'Not authenticated' when there is no token", async () => {
    await expect(
      fetchJsonWithStatus({ ...CONFIG, getToken: async () => null }, "/container/status"),
    ).rejects.toMatchObject({ status: 401, message: "Not authenticated" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("throws ApiError with detail message on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(409, { detail: "workspace pending" }));

    await expect(fetchJsonWithStatus(CONFIG, "/container/status")).rejects.toMatchObject({
      status: 409,
      message: "workspace pending",
    });
  });
});

describe("createApiClient", () => {
  it("serializes JSON bodies for post/put/patch and hits the right endpoints", async () => {
    // Fresh Response per call — a body is single-read.
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));
    const client = createApiClient(CONFIG);

    await client.post("/agents", { name: "atlas" });
    await client.patchConfig({ agents: { defaults: {} } });
    await client.del("/agents/a1");

    expect(fetchMock.mock.calls[0][0]).toBe("https://api-test.isol8.co/api/v1/agents");
    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ name: "atlas" }));
    expect(fetchMock.mock.calls[1][0]).toBe("https://api-test.isol8.co/api/v1/config");
    expect(fetchMock.mock.calls[1][1].body).toBe(JSON.stringify({ patch: { agents: { defaults: {} } } }));
    expect(fetchMock.mock.calls[2][1].method).toBe("DELETE");
  });

  it("del sends a JSON body only when given one", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));
    const client = createApiClient(CONFIG);
    await client.del("/apps/a1", undefined, { confirm_name: "Tracker" });
    await client.del("/agents/a1");
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ confirm_name: "Tracker" }));
    expect(fetchMock.mock.calls[1][1].body).toBeUndefined();
  });

  it("resolves a 204 no-content response as null instead of failing JSON parse", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = createApiClient(CONFIG);

    await expect(client.del("/container/brain/file?path=a.md")).resolves.toBeNull();
  });

  it("downloadBlob returns bytes and throws ApiError on failure", async () => {
    fetchMock.mockResolvedValueOnce(new Response(new Blob(["abc"]), { status: 200 }));
    const client = createApiClient(CONFIG);

    const blob = await client.downloadBlob("/workspace/file");
    expect(await blob.text()).toBe("abc");

    fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: "gone" }));
    await expect(client.downloadBlob("/workspace/file")).rejects.toMatchObject({
      status: 404,
      message: "gone",
    });
  });
});
