import { renderHook, act } from "@testing-library/react-native";
import { AppState } from "react-native";

// The real provider memoizes its context value, and every field on it is an
// effect dependency inside the shared hook — so the mock returns ONE object,
// not a fresh one per call. A per-render `live` re-fires the history reconcile.
const mockLive = {
  messaging: {},
  onEvent: () => () => {},
  claimActiveRoomSubscription: () => () => {},
  liveChannels: [],
  connectionRevision: 0,
  liveConnected: false,
  httpReady: true,
};

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useOrganization: () => ({ organization: { id: "org-a" } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "22222222-2222-4222-8222-222222222222" }));
jest.mock("../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import { useRoomsPlatform } from "../rooms";

describe("useRoomsPlatform", () => {
  // jest-expo's AppState.addEventListener returns undefined, not a subscription;
  // stub it so `.remove()` on unsubscribe is exercised the way a device behaves.
  beforeEach(() => {
    jest.spyOn(AppState, "addEventListener").mockReturnValue({ remove: () => {} } as never);
  });
  afterEach(() => jest.restoreAllMocks());

  it("reports identity, org and a native uuid", async () => {
    const { result } = await renderHook(() => useRoomsPlatform());
    expect(result.current.userId).toBe("user-a");
    expect(result.current.orgId).toBe("org-a");
    expect(result.current.uuid()).toBe("22222222-2222-4222-8222-222222222222");
  });

  it("is visible only while AppState is active and the screen says so", async () => {
    const appState = AppState as { currentState: string | null };
    const original = appState.currentState;
    appState.currentState = "active";
    const { result, rerender } = await renderHook(({ v }: { v: boolean }) => useRoomsPlatform(v), { initialProps: { v: true } });
    expect(result.current.isVisible()).toBe(true);
    appState.currentState = "background";
    expect(result.current.isVisible()).toBe(false);
    appState.currentState = "active";
    await rerender({ v: false });
    expect(result.current.isVisible()).toBe(false);
    appState.currentState = original;
  });

  it("notifies visibility listeners on an AppState change and removes them on unsubscribe", async () => {
    const listeners: ((state: string) => void)[] = [];
    const remove = jest.fn();
    const spy = jest.spyOn(AppState, "addEventListener").mockImplementation(((_: string, cb: (s: string) => void) => {
      listeners.push(cb);
      return { remove } as never;
    }) as never);
    const { result } = await renderHook(() => useRoomsPlatform());
    const seen = jest.fn();
    const stop = result.current.onVisibilityChange(seen);
    await act(async () => listeners[0]("active"));
    expect(seen).toHaveBeenCalledTimes(1);
    stop();
    expect(remove).toHaveBeenCalled();
    spy.mockRestore();
  });

  it("fires visibility listeners when the screen's own visible prop changes", async () => {
    const { result, rerender } = await renderHook(({ v }: { v: boolean }) => useRoomsPlatform(v), { initialProps: { v: false } });
    const seen = jest.fn();
    const stop = result.current.onVisibilityChange(seen);
    const platform = result.current;
    await rerender({ v: true });
    expect(seen).toHaveBeenCalledTimes(1);
    // visible lives on a ref, so the platform object itself does not churn.
    expect(result.current).toBe(platform);
    stop();
  });

  it("fires nothing on a later visible change once the listener has unsubscribed", async () => {
    const { result, rerender } = await renderHook(({ v }: { v: boolean }) => useRoomsPlatform(v), { initialProps: { v: false } });
    const seen = jest.fn();
    result.current.onVisibilityChange(seen)();
    await rerender({ v: true });
    expect(seen).not.toHaveBeenCalled();
  });

  it("keeps onVisibilityChange referentially stable across renders", async () => {
    const { result, rerender } = await renderHook(() => useRoomsPlatform());
    const first = result.current.onVisibilityChange;
    await rerender({});
    expect(result.current.onVisibilityChange).toBe(first);
  });

  it("keeps the platform object itself stable when its inputs do not change", async () => {
    const { result, rerender } = await renderHook(() => useRoomsPlatform());
    const first = result.current;
    await rerender({});
    expect(result.current).toBe(first);
  });
});

// `report`/`stop` close over refs only — calling them updates no React state,
// so they run as plain function calls below, never wrapped in `act()`; the
// hook never triggers a re-render on its own.
