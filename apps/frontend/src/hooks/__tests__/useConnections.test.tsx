import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useConnections } from "../useConnections";
import { ApiError } from "@/lib/api";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

// pollUntil sleeps 3 s between checks; the hook's contract (stop on connected,
// stop with the message on failed, give up on timeout) is what matters here.
vi.mock("@/lib/externalAuth", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/externalAuth")>();
  return {
    ...mod,
    pollUntil: async (check: () => Promise<boolean>) => {
      for (let i = 0; i < 6; i += 1) {
        if (await check()) return true;
      }
      return false;
    },
  };
});

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useConnections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("exposes the server-driven connections array", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        connections: [
          { provider: "linear", display_name: "Linear", acquisition: "paste_token", status: "disconnected" },
          { provider: "google", display_name: "Google", acquisition: "direct_oauth", status: "connected" },
        ],
      }),
    );
    const { result } = renderHook(() => useConnections(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.connections).toHaveLength(2);
    expect(result.current.connections[0].provider).toBe("linear");
  });

  it("surfaces a 422 from connectPaste so the caller can show validation copy", async () => {
    const fetchMock = vi
      .fn()
      // initial GET /connections
      .mockResolvedValueOnce(jsonResponse({ connections: [] }))
      // POST /connections/linear → 422
      .mockResolvedValueOnce(
        jsonResponse({ detail: "token failed validation against the provider" }, 422),
      );
    global.fetch = fetchMock;

    const { result } = renderHook(() => useConnections(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await expect(
      act(() => result.current.connectPaste("linear", "bad-token")),
    ).rejects.toMatchObject({ status: 422 });
  });

  it("redirects the browser to authorize_url on startOAuth", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ connections: [] }))
      .mockResolvedValueOnce(jsonResponse({ authorize_url: "https://provider.test/oauth" }));
    global.fetch = fetchMock;

    // window.location.href is read-only in jsdom; redefine the property with
    // a settable stub so startOAuth's assignment is observable.
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { href: "" } as Location,
    });

    try {
      const { result } = renderHook(() => useConnections(), { wrapper });
      await waitFor(() => expect(result.current.isLoading).toBe(false));
      await act(() => result.current.startOAuth("google"));
      expect(window.location.href).toBe("https://provider.test/oauth");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: original,
      });
    }
  });

  it("re-exports ApiError for callers that switch on status", () => {
    expect(new ApiError(403, null).status).toBe(403);
  });

  describe("desktop (Tauri)", () => {
    afterEach(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      delete (window as any).__TAURI__;
    });

    it("startOAuth rejects — and never enters the poll — when the browser can't be opened", async () => {
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ connections: [] }))
        .mockResolvedValueOnce(jsonResponse({ authorize_url: "https://provider.test/oauth" }));
      global.fetch = fetchMock;
      const invoke = vi.fn().mockRejectedValue("forbidden");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__TAURI__ = { core: { invoke } };

      const { result } = renderHook(() => useConnections(), { wrapper });
      await waitFor(() => expect(result.current.isLoading).toBe(false));

      // Capture the rejection inside act — `.rejects` on an act-wrapped
      // promise is racy under React 19's async act (the act promise can
      // resolve before the callback rejection lands, leaving an unhandled
      // late assertion in CI).
      let err: unknown = null;
      await act(async () => {
        err = await result.current.startOAuth("cloudflare").then(
          () => null,
          (e) => e,
        );
      });
      expect(err).toBeInstanceOf(Error);
      expect((err as Error).message).toBe(
        "Couldn't open your browser. Try again.",
      );
      // No waiting state: only the initial /connections GET and the
      // authorize GET happened — no poll re-fetches of /connections.
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it("startOAuth opens the system browser and resolves once the grant lands", async () => {
      const disconnected = {
        connections: [
          { provider: "cloudflare", display_name: "Cloudflare", acquisition: "direct_oauth", status: "disconnected" },
        ],
      };
      const connected = {
        connections: [
          { provider: "cloudflare", display_name: "Cloudflare", acquisition: "direct_oauth", status: "connected" },
        ],
      };
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(disconnected))
        .mockResolvedValueOnce(jsonResponse({ authorize_url: "https://provider.test/oauth" }))
        // first poll re-fetch of /connections → already connected
        .mockResolvedValue(jsonResponse(connected));
      global.fetch = fetchMock;
      const invoke = vi.fn().mockResolvedValue(undefined);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).__TAURI__ = { core: { invoke } };

      const { result } = renderHook(() => useConnections(), { wrapper });
      await waitFor(() => expect(result.current.isLoading).toBe(false));

      await act(() => result.current.startOAuth("cloudflare"));
      expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
        path: "https://provider.test/oauth",
      });
      expect(result.current.connections[0].status).toBe("connected");
    });
  });

  it("startContainerLogin posts the store, polls status until connected, then refetches", async () => {
    const calls: string[] = [];
    let statusPolls = 0;
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/connections/shopify/login")) {
        return jsonResponse({ status: "awaiting_login", provider: "shopify", store: "mystore.myshopify.com" });
      }
      if (url.includes("/connections/shopify/login/status?store=mystore.myshopify.com")) {
        statusPolls += 1;
        return jsonResponse({ status: statusPolls < 2 ? "pending" : "connected" });
      }
      return jsonResponse({ connections: [] });
    }) as unknown as typeof fetch;

    const { result } = renderHook(() => useConnections(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await act(() => result.current.startContainerLogin("shopify", "mystore.myshopify.com"));

    expect(calls.some((c) => c.startsWith("POST") && c.endsWith("/connections/shopify/login"))).toBe(true);
    expect(statusPolls).toBe(2);
    // The SWR row is refetched once the CLI holds the session.
    expect(calls.filter((c) => c.startsWith("GET") && c.endsWith("/connections")).length).toBeGreaterThanOrEqual(2);
  });

  it("startContainerLogin surfaces the CLI's failure instead of polling on", async () => {
    global.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/connections/shopify/login")) {
        return jsonResponse({ status: "awaiting_login", provider: "shopify", store: "mystore.myshopify.com" });
      }
      if (url.includes("/login/status")) {
        return jsonResponse({ status: "failed", error: "Timed out waiting for OAuth callback." });
      }
      return jsonResponse({ connections: [] });
    }) as unknown as typeof fetch;

    const { result } = renderHook(() => useConnections(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    // Caught inside act: a rejection propagated THROUGH act re-renders the hook
    // first, and this file's Clerk mock mints a fresh getToken per render, so
    // the assertion would see the re-render's auth error instead of ours.
    let caught: unknown;
    await act(async () => {
      try {
        await result.current.startContainerLogin("shopify", "mystore.myshopify.com");
      } catch (e) {
        caught = e;
      }
    });
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toMatch(/Timed out waiting for OAuth callback/);
  });
});
