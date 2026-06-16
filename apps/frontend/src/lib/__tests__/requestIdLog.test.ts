import { describe, it, expect, beforeEach } from "vitest";

import { recordRequestId, getRecentRequestIds, clearRequestIds } from "../requestIdLog";

beforeEach(() => clearRequestIds());

describe("requestIdLog", () => {
  it("returns recorded ids newest-first", () => {
    recordRequestId("a");
    recordRequestId("b");
    recordRequestId("c");
    expect(getRecentRequestIds()).toEqual(["c", "b", "a"]);
  });

  it("ignores null / undefined / empty", () => {
    recordRequestId(null);
    recordRequestId(undefined);
    recordRequestId("");
    expect(getRecentRequestIds()).toEqual([]);
  });

  it("dedupes a consecutive duplicate (a retry replays the same id)", () => {
    recordRequestId("a");
    recordRequestId("a");
    recordRequestId("b");
    expect(getRecentRequestIds()).toEqual(["b", "a"]);
  });

  it("keeps non-consecutive repeats", () => {
    recordRequestId("a");
    recordRequestId("b");
    recordRequestId("a");
    expect(getRecentRequestIds()).toEqual(["a", "b", "a"]);
  });

  it("caps at 20, dropping the oldest", () => {
    for (let i = 0; i < 25; i++) recordRequestId(`r${i}`);
    const ids = getRecentRequestIds();
    expect(ids).toHaveLength(20);
    expect(ids[0]).toBe("r24"); // newest first
    expect(ids[19]).toBe("r5"); // r0..r4 dropped
  });
});
