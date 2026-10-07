import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { useCountdown } from "../useCountdown";

function Consumer({ iso }: { iso: string }) {
  const countdown = useCountdown(iso) ?? "—";
  return <span>{countdown}</span>;
}

describe("useCountdown", () => {
  it("renders the stable placeholder on the server, before any effect can run", () => {
    const future = new Date(Date.now() + 3_600_000).toISOString();
    const html = renderToStaticMarkup(<Consumer iso={future} />);
    expect(html).toContain("—");
  });

  it("resolves to a live countdown once mounted, and ticks on an interval", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
      const future = new Date(Date.now() + 3_661_000).toISOString(); // 1h 1m 1s out
      const { result } = renderHook(() => useCountdown(future));
      expect(result.current).toBe("1h 1m 1s");
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(result.current).toBe("1h 1m 0s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("floors at zero once the target has passed", () => {
    const past = new Date(Date.now() - 1000).toISOString();
    const { result } = renderHook(() => useCountdown(past));
    expect(result.current).toBe("0h 0m 0s");
  });
});
