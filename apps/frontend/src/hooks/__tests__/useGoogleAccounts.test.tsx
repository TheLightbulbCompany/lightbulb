import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useGoogleAccounts } from "../useGoogleAccounts";

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

describe("useGoogleAccounts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("exposes accounts and assignments from the server", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      jsonResponse({
        accounts: [
          { email: "a@x.com", connected_at: "2026-06-01T00:00:00Z" },
          { email: "b@x.com", connected_at: "2026-06-02T00:00:00Z" },
        ],
        assignments: { agent_1: "b@x.com" },
      }),
    );
    const { result } = renderHook(() => useGoogleAccounts(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.accounts).toHaveLength(2);
    expect(result.current.assignments).toEqual({ agent_1: "b@x.com" });
  });

  it("applies the returned assignment map after assign()", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ accounts: [{ email: "a@x.com", connected_at: "" }], assignments: {} }),
      )
      // POST /google/accounts/a@x.com/assign → new map
      .mockResolvedValueOnce(jsonResponse({ assignments: { agent_1: "a@x.com" } }));
    global.fetch = fetchMock;

    const { result } = renderHook(() => useGoogleAccounts(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(() => result.current.assign("a@x.com", "agent_1"));
    await waitFor(() =>
      expect(result.current.assignments).toEqual({ agent_1: "a@x.com" }),
    );
  });

  it("applies the returned assignment map after unassign()", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          accounts: [{ email: "a@x.com", connected_at: "" }],
          assignments: { agent_1: "a@x.com" },
        }),
      )
      // DELETE /google/accounts/assign/agent_1 → empty map
      .mockResolvedValueOnce(jsonResponse({ assignments: {} }));
    global.fetch = fetchMock;

    const { result } = renderHook(() => useGoogleAccounts(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(() => result.current.unassign("agent_1"));
    await waitFor(() => expect(result.current.assignments).toEqual({}));
  });

  it("redirects the browser to authorize_url on connectAnother()", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ accounts: [], assignments: {} }))
      .mockResolvedValueOnce(jsonResponse({ authorize_url: "https://google.test/oauth" }));
    global.fetch = fetchMock;

    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { href: "" } as Location,
    });

    try {
      const { result } = renderHook(() => useGoogleAccounts(), { wrapper });
      await waitFor(() => expect(result.current.isLoading).toBe(false));
      await act(() => result.current.connectAnother());
      expect(window.location.href).toBe("https://google.test/oauth");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: original,
      });
    }
  });

  it("surfaces a 403 from disconnectAccount so the caller can show it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ accounts: [{ email: "a@x.com", connected_at: "" }], assignments: {} }),
      )
      .mockResolvedValueOnce(jsonResponse({ detail: "forbidden" }, 403));
    global.fetch = fetchMock;

    const { result } = renderHook(() => useGoogleAccounts(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await expect(
      act(() => result.current.disconnectAccount("a@x.com")),
    ).rejects.toMatchObject({ status: 403 });
  });
});
