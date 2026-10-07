import { describe, it, expect, vi, beforeEach, afterAll } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";

const syncUser = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ syncUser }) }));

import { useUserSyncOnce } from "../useUserSyncOnce";

const restore = installStoragePolyfill();
afterAll(restore);
beforeEach(() => {
  syncUser.mockReset();
  window.sessionStorage.clear();
});

describe("useUserSyncOnce", () => {
  it("does nothing while disabled", () => {
    renderHook(() => useUserSyncOnce(false));
    expect(syncUser).not.toHaveBeenCalled();
  });

  it("syncs exactly once when enabled, across rerenders, and sets the session marker", async () => {
    syncUser.mockResolvedValue(undefined);
    const { rerender } = renderHook(({ on }) => useUserSyncOnce(on), {
      initialProps: { on: true },
    });
    rerender({ on: true });
    rerender({ on: true });
    await waitFor(() => expect(window.sessionStorage.getItem("isol8:user-synced")).toBe("1"));
    expect(syncUser).toHaveBeenCalledTimes(1);
  });

  it("skips when the session marker is already set", () => {
    window.sessionStorage.setItem("isol8:user-synced", "1");
    renderHook(() => useUserSyncOnce(true));
    expect(syncUser).not.toHaveBeenCalled();
  });

  it("releases the latch on failure so a remount can retry", async () => {
    syncUser.mockRejectedValueOnce(new Error("boom"));
    const consoleErr = vi.spyOn(console, "error").mockImplementation(() => {});
    const first = renderHook(() => useUserSyncOnce(true));
    await waitFor(() => expect(syncUser).toHaveBeenCalledTimes(1));
    expect(window.sessionStorage.getItem("isol8:user-synced")).toBeNull();
    first.unmount();
    syncUser.mockResolvedValue(undefined);
    renderHook(() => useUserSyncOnce(true));
    await waitFor(() => expect(syncUser).toHaveBeenCalledTimes(2));
    consoleErr.mockRestore();
  });
});
