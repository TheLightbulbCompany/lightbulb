import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, post: mockPost, patch: mockPatch, del: mockDel }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

// The opts the Teams layer must forward on every BFF call so the browser
// attaches the `.thelightbulb.company` Paperclip cookie and retries once on a rotated
// session.
const EXPECTED_OPTS = { credentials: "include", retryOn401: true };

describe("useTeamsApi cookie opt-ins", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
  });

  it("forwards credentials + retryOn401 on reads, prefixing /teams", async () => {
    mockGet.mockResolvedValue({ ok: true });

    // `read` is itself a hook (wraps useSWR), so call it at the top of a
    // rendered hook body — not inside a nested function.
    renderHook(() => useTeamsApi().read("/dashboard"), { wrapper });

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith("/teams/dashboard", EXPECTED_OPTS);
    });
  });

  it("forwards opts on post/patch/del", async () => {
    mockPost.mockResolvedValue({ ok: true });
    mockPatch.mockResolvedValue({ ok: true });
    mockDel.mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useTeamsApi(), { wrapper });

    await result.current.post("/agents", { name: "a" });
    await result.current.patch("/agents/1", { name: "b" });
    await result.current.del("/agents/1");

    expect(mockPost).toHaveBeenCalledWith("/teams/agents", { name: "a" }, EXPECTED_OPTS);
    expect(mockPatch).toHaveBeenCalledWith("/teams/agents/1", { name: "b" }, EXPECTED_OPTS);
    expect(mockDel).toHaveBeenCalledWith("/teams/agents/1", EXPECTED_OPTS);
  });

  it("uses a unique SWR cache key per path", async () => {
    mockGet.mockImplementation((p: string) =>
      Promise.resolve(p === "/teams/agents" ? { list: "agents" } : { list: "issues" }),
    );

    function Both() {
      const api = useTeamsApi();
      const a = api.read<{ list: string }>("/agents");
      const b = api.read<{ list: string }>("/issues");
      return { a, b };
    }

    const { result } = renderHook(Both, { wrapper });

    await waitFor(() => expect(result.current.a.data).toEqual({ list: "agents" }));
    await waitFor(() => expect(result.current.b.data).toEqual({ list: "issues" }));
    expect(mockGet).toHaveBeenCalledWith("/teams/agents", EXPECTED_OPTS);
    expect(mockGet).toHaveBeenCalledWith("/teams/issues", EXPECTED_OPTS);
  });

  it("propagates fetcher errors to SWR.error", async () => {
    const boom = new Error("boom");
    mockGet.mockRejectedValueOnce(boom);
    const { result } = renderHook(
      () => useTeamsApi().read("/agents", { shouldRetryOnError: false }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.error).toBe(boom));
  });

  it("still re-throws the 202 provisioning sentinel from a read body", async () => {
    mockGet.mockResolvedValue({ detail: "team workspace provisioning" });

    const { result } = renderHook(
      () => useTeamsApi().read<unknown>("/dashboard", { shouldRetryOnError: false }),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.error).toBeDefined();
    });
    expect((result.current.error as { status?: number }).status).toBe(202);
  });
});
