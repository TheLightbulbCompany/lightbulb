import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = { get: vi.fn(), post: vi.fn() };
let present = true;
let visibility: DocumentVisibilityState = "visible";
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("@/lib/presence", () => ({ isPresent: () => present }));
vi.mock("swr", () => ({ default: (key: unknown) => ({ data: key ? { online: ["alice"] } : undefined }) }));

import { usePresence } from "../usePresence";

beforeEach(() => {
  vi.useFakeTimers();
  api.get.mockResolvedValue({ online: [] });
  api.post.mockResolvedValue(undefined);
  present = true;
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

it("heartbeats on mount and every 45s while the tab is visible and in use", () => {
  const { result } = renderHook(() => usePresence(true, "org_1"));
  expect(api.post).toHaveBeenCalledTimes(1);
  act(() => { vi.advanceTimersByTime(45_000); });
  expect(api.post).toHaveBeenCalledTimes(2);
  expect(api.post).toHaveBeenLastCalledWith("/messaging/presence", {});
  expect(result.current.has("alice")).toBe(true);
});

it("stays silent while the tab is hidden or nobody has touched it, and beats again when it comes back", () => {
  visibility = "hidden";
  renderHook(() => usePresence(true, "org_1"));
  act(() => { vi.advanceTimersByTime(90_000); });
  expect(api.post).not.toHaveBeenCalled();
  visibility = "visible";
  present = false;
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  expect(api.post).not.toHaveBeenCalled();
  present = true;
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  expect(api.post).toHaveBeenCalledTimes(1);
});

it("does nothing until enabled, and stops on unmount", () => {
  const { result, unmount } = renderHook(() => usePresence(false, "org_1"));
  act(() => { vi.advanceTimersByTime(90_000); });
  expect(api.post).not.toHaveBeenCalled();
  expect(result.current.size).toBe(0);
  const live = renderHook(() => usePresence(true, "org_1"));
  expect(api.post).toHaveBeenCalledTimes(1);
  live.unmount();
  unmount();
  act(() => { vi.advanceTimersByTime(90_000); });
  expect(api.post).toHaveBeenCalledTimes(1);
});
