import { act, renderHook } from "@testing-library/react-native";
import { AppState } from "react-native";

const postSpy = jest.fn(async () => ({}));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
}));
jest.mock("@isol8/shared", () => ({ createApiClient: () => ({ post: postSpy }) }));
jest.mock("../adapter", () => ({ makeAdapter: () => ({}) }));

import { useContainerWake } from "../containerWake";

function setActive(state: string) {
  (AppState as unknown as { currentState: string }).currentState = state;
}

// The wake cooldown lives in module scope (one app, one wake cadence), so it
// persists across tests. Drive Date.now explicitly and jump it past the 20s
// cooldown in each test so ordering can't make a first call spuriously dedupe.
let now = 1_000_000;

describe("useContainerWake", () => {
  beforeEach(() => {
    postSpy.mockClear();
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
    expect(postSpy).toHaveBeenCalledWith("/container/wake", { reason: "mount" });
  });

  it("suppresses a passive wake while backgrounded", async () => {
    setActive("background");
    const { result } = await renderHook(() => useContainerWake());
    await act(async () => {
      await result.current.requestWake("mount");
    });
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("dedupes repeat calls within the cooldown", async () => {
    const { result } = await renderHook(() => useContainerWake());
    await act(async () => {
      await result.current.requestWake("interaction");
      await result.current.requestWake("interaction"); // same `now` → cooldown
    });
    expect(postSpy).toHaveBeenCalledTimes(1);
  });
});
