import { act, renderHook } from "@testing-library/react-native";
import { AppState } from "react-native";

// Spy created INSIDE the factory (no out-of-scope reference — jest hoists
// jest.mock above imports, so an outer const would be in the TDZ when the
// factory runs). Retrieved via jest.requireMock below.
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
}));
jest.mock("@isol8/shared", () => {
  const post = jest.fn(async () => ({}));
  return { createApiClient: () => ({ post }), __post: post };
});
jest.mock("../adapter", () => ({ makeAdapter: () => ({}) }));

import { useContainerWake } from "../containerWake";

const { __post: mockPost } = jest.requireMock("@isol8/shared") as {
  __post: jest.Mock;
};

function setActive(state: string) {
  (AppState as unknown as { currentState: string }).currentState = state;
}

// The wake cooldown lives in module scope (one app, one wake cadence), so it
// persists across tests. Drive Date.now explicitly and jump it past the 20s
// cooldown in each test so ordering can't make a first call spuriously dedupe.
let now = 1_000_000;

describe("useContainerWake", () => {
  beforeEach(() => {
    mockPost.mockClear();
    setActive("active");
    now += 60_000;
    jest.spyOn(Date, "now").mockImplementation(() => now);
  });

  afterEach(() => {
    (Date.now as jest.Mock).mockRestore();
  });

  it("posts /container/wake for a passive reason while foregrounded", async () => {
    const { result } = await renderHook(() => useContainerWake());
    await act(async () => {
      await result.current.requestWake("mount");
    });
    expect(mockPost).toHaveBeenCalledWith("/container/wake", { reason: "mount" });
  });

  it("suppresses a passive wake while backgrounded", async () => {
    setActive("background");
    const { result } = await renderHook(() => useContainerWake());
    await act(async () => {
      await result.current.requestWake("mount");
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("dedupes repeat calls within the cooldown", async () => {
    const { result } = await renderHook(() => useContainerWake());
    await act(async () => {
      await result.current.requestWake("interaction");
      await result.current.requestWake("interaction"); // same `now` → cooldown
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
  });
});
