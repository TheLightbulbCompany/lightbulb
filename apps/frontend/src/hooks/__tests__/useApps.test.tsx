import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

import { useApps, type AppRow } from "@/hooks/useApps";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();

const api = { get: mockGet, post: mockPost, patch: mockPatch, del: mockDel }; // stable, as useApi's useMemo is
vi.mock("@/lib/api", () => ({ useApi: () => api }));
let liveHandler: ((e: unknown) => void) | null = null;
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ onEvent: (h: (e: unknown) => void) => { liveHandler = h; return () => { liveHandler = null; }; } }),
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, userId: "u1", orgId: "o1" }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
import { notifyApiError } from "@/lib/notify";

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

  it("refetches at once on an apps invalidation, and ignores other resources", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    const n = mockGet.mock.calls.length;
    await act(async () => { liveHandler?.({ type: "runtime.resource.invalidated", payload: { resource: "mail" } }); });
    expect(mockGet.mock.calls.length).toBe(n);
    await act(async () => { liveHandler?.({ type: "runtime.resource.invalidated", payload: { resource: "apps", resourceId: "app_1" } }); });
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThan(n));
  });

  it("opens an app's conversation, keeps the function stable, and re-reads the list so its conversation_id lands", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockPost.mockResolvedValue({ room_id: "conv-1" });
    const { result, rerender } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    const first = result.current.openConversation;
    rerender();
    expect(result.current.openConversation).toBe(first); // SessionChat re-opens when it changes
    const n = mockGet.mock.calls.length;
    let out: { roomId: string } | undefined;
    await act(async () => { out = await result.current.openConversation("app_1"); });
    expect(out).toEqual({ roomId: "conv-1" });
    expect(mockPost).toHaveBeenCalledWith("/apps/app_1/conversation", {});
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThan(n));
  });

  it("hides removed (destroyed) apps", async () => {
    mockGet.mockResolvedValue({ apps: [row(), row({ app_id: "app_2", state: "destroyed" })] });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    expect(result.current.apps[0].app_id).toBe("app_1");
  });

  it("polls every 15 s while an app is awaiting approval, provisioning or building", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGet.mockResolvedValue({ apps: [row({ state: "provisioning" })] });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    const n = mockGet.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15_100); });
    expect(mockGet.mock.calls.length).toBeGreaterThan(n);
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

  it.each(["building", "requested"] as const)("polls while an app is %s", async (state) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGet.mockResolvedValue({ apps: [row({ state })] });
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

  it("publish posts to /apps/{id}/publish", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockPost.mockResolvedValue({ app_id: "app_1", state: "building" });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    await expect(result.current.publish("app_1")).resolves.toMatchObject({ state: "building" });
    expect(mockPost).toHaveBeenCalledWith("/apps/app_1/publish", {});
    mockPost.mockRejectedValue(new Error("No ready preview"));
    await expect(result.current.publish("app_1")).rejects.toThrow("No ready preview");
  });

  it("listInvites reads the emails; revokeInvite sends one and toasts a failure", async () => {
    mockGet.mockResolvedValue({ apps: [], emails: ["a@x.com"] });
    mockDel.mockResolvedValue({ revoked: 1 });
    const { result } = renderHook(() => useApps(), { wrapper });
    await expect(result.current.listInvites("a b")).resolves.toEqual(["a@x.com"]);
    expect(mockGet).toHaveBeenCalledWith("/apps/a%20b/invites");
    await result.current.revokeInvite("app_1", "a@x.com");
    expect(mockDel).toHaveBeenCalledWith("/apps/app_1/invites", undefined, { email: "a@x.com" });
    mockDel.mockRejectedValue(new Error("no"));
    await expect(result.current.revokeInvite("app_1", "a@x.com")).rejects.toThrow("no");
    expect(notifyApiError).toHaveBeenCalledWith(expect.any(Error), "The invite could not be removed. Try again.");
  });

  it("askAgent posts only an error it was given, capped to what the route takes", async () => {
    mockGet.mockResolvedValue({ apps: [] });
    mockPost.mockResolvedValue({ asked: true });
    const { result } = renderHook(() => useApps(), { wrapper });
    await result.current.askAgent("app_1");
    expect(mockPost).toHaveBeenLastCalledWith("/apps/app_1/ask-agent", {});
    await result.current.askAgent("app_1", "x".repeat(2500));
    expect(mockPost).toHaveBeenLastCalledWith("/apps/app_1/ask-agent", { error: "x".repeat(2000) });
  });

  it("rollback names the version, then re-reads the list", async () => {
    mockGet.mockResolvedValue({ apps: [row()] });
    mockPost.mockResolvedValue({ state: "live" });
    const { result } = renderHook(() => useApps(), { wrapper });
    await waitFor(() => expect(result.current.apps).toHaveLength(1));
    const n = mockGet.mock.calls.length;
    await act(() => result.current.rollback("app_1", 2));
    expect(mockPost).toHaveBeenCalledWith("/apps/app_1/rollback", { version: 2 });
    expect(mockGet.mock.calls.length).toBeGreaterThan(n);
    mockPost.mockRejectedValue(new Error("no"));
    await expect(result.current.rollback("app_1", 2)).rejects.toThrow("no");
  });
});
