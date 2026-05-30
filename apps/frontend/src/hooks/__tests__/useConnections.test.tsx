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
});
