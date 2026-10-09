import { act, renderHook } from "@testing-library/react-native";
import { AppState } from "react-native";

import { isPresent, usePresence } from "../presence";

describe("presence", () => {
  it("is present only when AppState is active", () => {
    (AppState as unknown as { currentState: string }).currentState = "active";
    expect(isPresent()).toBe(true);
    (AppState as unknown as { currentState: string }).currentState = "background";
    expect(isPresent()).toBe(false);
    (AppState as unknown as { currentState: string }).currentState = "inactive";
    expect(isPresent()).toBe(false);
  });
});

const mockGet = jest.fn(async () => ({ online: ["user_b"] }));
const mockPost = jest.fn(async () => ({}));
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: mockGet, post: mockPost }),
}));

describe("usePresence", () => {
  it("heartbeats while active and reports who is online", async () => {
    (AppState as unknown as { currentState: string }).currentState = "active";
    jest.useFakeTimers();
    const { result } = await renderHook(() => usePresence(true));
    await act(async () => {});
    expect(mockPost).toHaveBeenCalledWith("/messaging/presence", {});
    expect(result.current.has("user_b")).toBe(true);
    await act(async () => {
      jest.advanceTimersByTime(45_000);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    jest.useRealTimers();
  });

  it("stays silent while disabled", async () => {
    mockPost.mockClear();
    await renderHook(() => usePresence(false));
    expect(mockPost).not.toHaveBeenCalled();
  });
});
