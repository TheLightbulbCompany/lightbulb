// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useDesktopAuth } from "../useDesktopAuth";

const auth = vi.hoisted(() => ({ isLoaded: false, isSignedIn: false, userId: null as string | null }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth, useSignIn: () => ({}) }));
const invoke = vi.fn().mockResolvedValue(undefined);
beforeEach(() => {
  invoke.mockClear();
  Object.assign(auth, { isLoaded: false, isSignedIn: false, userId: null });
  Object.assign(window, { __TAURI__: { core: { invoke } } });
});
it("does not treat loading or unmount as logout; reports only actual human identity", () => {
  const { rerender, unmount } = renderHook(() => useDesktopAuth());
  expect(invoke).not.toHaveBeenCalled();
  Object.assign(auth, { isLoaded: true, isSignedIn: true, userId: "alice" });
  rerender();
  expect(invoke).toHaveBeenLastCalledWith("local_sessions_auth", { humanUserId: "alice" });
  Object.assign(auth, { isLoaded: false });
  rerender();
  expect(invoke).toHaveBeenCalledTimes(1);
  Object.assign(auth, { isLoaded: true, isSignedIn: false, userId: null });
  rerender();
  expect(invoke).toHaveBeenLastCalledWith("local_sessions_auth", { humanUserId: null });
  unmount();
  expect(invoke).toHaveBeenCalledTimes(2);
});
