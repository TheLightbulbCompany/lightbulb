import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import { useApps, type AppRow } from "@/hooks/useApps";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: mockGet, post: mockPost, patch: mockPatch, del: mockDel }),
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "u1", orgId: "o1" }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

function wrapper({ children }: { children: ReactNode }) {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{children}</SWRConfig>;
}

const row = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "app_1", name: "Tracker", room_id: "r1", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://tracker.apps.test", live_version: 1,
  last_opened_at: null, spend_month_cents: 0, can_open: true, ...over,
});

describe("useApps", () => {
  beforeEach(() => { mockGet.mockReset(); mockPost.mockReset(); mockPatch.mockReset(); mockDel.mockReset(); });
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

  it("invite posts the emails to the app's invites", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockPost.mockResolvedValue({});
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    await act(() => result.current.invite("app_1", ["a@x.com", "b@x.com"]));
    expect(mockPost).toHaveBeenCalledWith("/apps/app_1/invites", { emails: ["a@x.com", "b@x.com"] });
  });

  it("remove sends the confirmation name as a DELETE body and refreshes", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockDel.mockResolvedValue(null);
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    await act(() => result.current.remove("app_1", "Tracker"));
    expect(mockDel).toHaveBeenCalledWith("/apps/app_1", undefined, { confirm_name: "Tracker" });
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThan(1));
  });

  it("databaseLink uses the prod neon resource, else the first neon one", async () => {
    mockGet.mockResolvedValue({ apps: [] });
    mockPost.mockResolvedValue({ url: "https://console.neon.tech/x" });
    const { result } = renderHook(() => useApps(), { wrapper });
    const app = row({ resources: [
      { provider: "neon", service: "db", state: "ready", resource_id: "n_dev", environment: "dev" },
      { provider: "neon", service: "db", state: "ready", resource_id: "n_prod", environment: "prod" },
    ] });
    await expect(result.current.databaseLink(app)).resolves.toBe("https://console.neon.tech/x");
    expect(mockPost).toHaveBeenCalledWith("/company/provisioning/resources/neon/n_prod/dashboard-link", {});
    await act(() => result.current.databaseLink(row({ resources: [app.resources![0]] })));
    expect(mockPost).toHaveBeenLastCalledWith("/company/provisioning/resources/neon/n_dev/dashboard-link", {});
  });
});
