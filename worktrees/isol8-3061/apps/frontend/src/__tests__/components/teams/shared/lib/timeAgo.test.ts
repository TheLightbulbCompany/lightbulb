import { describe, test, expect, vi, beforeEach, afterEach } from "vitest";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";

describe("timeAgo", () => {
  const NOW = new Date("2026-05-04T12:00:00.000Z").getTime();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  test("returns 'just now' for sub-minute deltas", () => {
    const t = new Date(NOW - 30 * 1000); // 30s ago
    expect(timeAgo(t)).toBe("just now");
  });

  test("formats minutes as 'Nm ago'", () => {
    const t = new Date(NOW - 5 * 60 * 1000); // 5m ago
    expect(timeAgo(t)).toBe("5m ago");
  });

  test("formats hours as 'Nh ago'", () => {
    const t = new Date(NOW - 2 * 60 * 60 * 1000); // 2h ago
    expect(timeAgo(t)).toBe("2h ago");
  });

  test("formats days as 'Nd ago'", () => {
    const t = new Date(NOW - 24 * 60 * 60 * 1000); // 1d ago
    expect(timeAgo(t)).toBe("1d ago");
  });

  test("accepts ISO string input", () => {
    const iso = new Date(NOW - 3 * 60 * 60 * 1000).toISOString();
    expect(timeAgo(iso)).toBe("3h ago");
  });

  test("formats months as 'Nmo ago' for large deltas", () => {
    const t = new Date(NOW - 60 * 24 * 60 * 60 * 1000); // 60 days ago
    expect(timeAgo(t)).toBe("2mo ago");
  });
});
