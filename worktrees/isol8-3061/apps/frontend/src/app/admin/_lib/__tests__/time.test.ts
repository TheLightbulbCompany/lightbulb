import { describe, expect, it } from "vitest";

import { formatRelativeTime } from "../time";

const NOW = Date.parse("2026-07-01T12:00:00.000Z");

describe("formatRelativeTime", () => {
  it("renders an em dash for absent values", () => {
    expect(formatRelativeTime(null, NOW)).toEqual({ label: "—", iso: null });
    expect(formatRelativeTime(undefined, NOW)).toEqual({ label: "—", iso: null });
    expect(formatRelativeTime("", NOW)).toEqual({ label: "—", iso: null });
  });

  it("passes unparseable strings through", () => {
    expect(formatRelativeTime("not-a-date", NOW)).toEqual({
      label: "not-a-date",
      iso: null,
    });
  });

  it("renders just now under a minute", () => {
    expect(formatRelativeTime("2026-07-01T11:59:30.000Z", NOW).label).toBe("just now");
  });

  it("renders minutes, hours, and days", () => {
    expect(formatRelativeTime("2026-07-01T11:57:00.000Z", NOW).label).toBe("3m ago");
    expect(formatRelativeTime("2026-07-01T07:00:00.000Z", NOW).label).toBe("5h ago");
    expect(formatRelativeTime("2026-06-29T12:00:00.000Z", NOW).label).toBe("2d ago");
  });

  it("falls back to the date beyond 7 days", () => {
    expect(formatRelativeTime("2026-06-10T12:00:00.000Z", NOW).label).toBe("2026-06-10");
  });

  it("falls back to the date for future timestamps", () => {
    expect(formatRelativeTime("2026-08-01T12:00:00.000Z", NOW).label).toBe("2026-08-01");
  });

  it("accepts Clerk ms-since-epoch numbers", () => {
    const result = formatRelativeTime(NOW - 120_000, NOW);
    expect(result.label).toBe("2m ago");
    expect(result.iso).toBe("2026-07-01T11:58:00.000Z");
  });

  it("always returns the absolute ISO for parseable values", () => {
    expect(formatRelativeTime("2026-07-01T11:57:00.000Z", NOW).iso).toBe(
      "2026-07-01T11:57:00.000Z",
    );
  });
});
