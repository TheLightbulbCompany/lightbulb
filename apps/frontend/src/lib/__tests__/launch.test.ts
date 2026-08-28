import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchLaunchInfo, fetchLeaderboard, formatMoney, formatPct } from "../launch";

describe("launch formatters", () => {
  it("formats whole-currency money", () => {
    expect(formatMoney(123456, "usd")).toBe("$1,235");
    expect(formatMoney(-2000, "usd")).toBe("-$20");
    expect(formatMoney(0, "eur")).toBe("€0");
  });
  it("formats percent with sign, and 'new' for no baseline", () => {
    expect(formatPct(0.25)).toBe("+25%");
    expect(formatPct(-0.04)).toBe("−4%");
    expect(formatPct(null)).toBe("new");
  });
});

describe("public launch reads", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // The routes send their own Cache-Control and the backend holds a short TTL;
  // `cache: "no-store"` here would defeat both and turn every page view into a
  // pair of unauthenticated full table scans.
  it("does not opt out of caching", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ entrants: 0, rows: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await fetchLaunchInfo();
    await fetchLeaderboard();
    for (const call of fetchMock.mock.calls) {
      expect(call[1]?.cache).toBeUndefined();
    }
  });
});
