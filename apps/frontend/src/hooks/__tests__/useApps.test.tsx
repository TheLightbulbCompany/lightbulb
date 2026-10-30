import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import { useApps, type AppRow } from "@/hooks/useApps";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, post: mockPost, patch: mockPatch }),
}));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

function wrapper({ children }: { children: ReactNode }) {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
}

const row = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "app_1", name: "Tracker", room_id: "r1", state: "live", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://tracker.apps.test", live_version: 1,
  last_opened_at: null, spend_month_cents: 0, can_open: true, ...over,
});

describe("useApps", () => {
  beforeEach(() => { mockGet.mockReset(); mockPost.mockReset(); mockPatch.mockReset(); });
  afterEach(() => vi.useRealTimers());

  it("lists apps from /apps", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    expect(mockGet).toHaveBeenCalledWith("/apps");
  });

  it("open posts and returns the url", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockPost.mockResolvedValue({ url: "https://x.test/a" });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    await expect(result.current.open("app_1")).resolves.toBe("https://x.test/a");
    expect(mockPost).toHaveBeenCalledWith("/apps/app_1/open", {});
    await act(() => result.current.open("app_1", "preview"));
    expect(mockPost).toHaveBeenLastCalledWith("/apps/app_1/open?target=preview", {});
  });

  it("update patches and refreshes", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockPatch.mockResolvedValue(row({ open_to: "workspace" }));
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    const updated = await act(() => result.current.update("app_1", { open_to: "workspace" }));
    expect(updated.open_to).toBe("workspace");
    expect(mockPatch).toHaveBeenCalledWith("/apps/app_1", { open_to: "workspace" });
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThan(1));
  });

  it("polls while an app is building", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGet.mockResolvedValue({ apps: [row({ state: "building" })] });
    renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(15_100); });
    expect(mockGet.mock.calls.length).toBeGreaterThan(1);
  });
});
