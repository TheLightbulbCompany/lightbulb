// Tests for useActivityFeed: empty / envelope-normalization / bare-array /
// limit slicing / error propagation paths.

import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import {
  useActivityFeed,
  type ActivityEvent,
} from "@/components/teams/dashboard/hooks/useActivityFeed";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUseTeamsApi = vi.mocked(useTeamsApi);

function makeRead(payload: {
  data?: unknown;
  isLoading?: boolean;
  error?: Error | null;
}) {
  return vi.fn(() => ({
    data: payload.data,
    isLoading: payload.isLoading ?? false,
    error: payload.error ?? null,
    mutate: vi.fn(),
    isValidating: false,
  }));
}

const baseApi = (read: ReturnType<typeof makeRead>) =>
  ({
    read,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }) as unknown as ReturnType<typeof useTeamsApi>;

const evt = (id: string): ActivityEvent => ({
  id,
  action: "issue.created",
  createdAt: "2026-05-05T00:00:00Z",
});

describe("useActivityFeed", () => {
  test("returns empty events when read returns undefined", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({})));
    const { result } = renderHook(() => useActivityFeed());
    expect(result.current.events).toEqual([]);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isError).toBe(false);
  });

  test("normalizes envelope { events: [...] }", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ data: { events: [evt("a"), evt("b")] } }))
    );
    const { result } = renderHook(() => useActivityFeed());
    expect(result.current.events).toHaveLength(2);
    expect(result.current.events[0].id).toBe("a");
  });

  test("normalizes envelope { items: [...] }", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ data: { items: [evt("c")] } }))
    );
    const { result } = renderHook(() => useActivityFeed());
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].id).toBe("c");
  });

  test("normalizes bare array", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ data: [evt("d"), evt("e"), evt("f")] }))
    );
    const { result } = renderHook(() => useActivityFeed());
    expect(result.current.events).toHaveLength(3);
  });

  test("slices to the provided limit", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          data: [evt("1"), evt("2"), evt("3"), evt("4"), evt("5")],
        })
      )
    );
    const { result } = renderHook(() => useActivityFeed(2));
    expect(result.current.events).toHaveLength(2);
    expect(result.current.events.map((e) => e.id)).toEqual(["1", "2"]);
  });

  test("returns isError: true when read returns an error", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ error: new Error("boom") }))
    );
    const { result } = renderHook(() => useActivityFeed());
    expect(result.current.isError).toBe(true);
    expect(result.current.events).toEqual([]);
  });

  test("calls read with the /activity path", () => {
    const read = makeRead({});
    mockUseTeamsApi.mockReturnValue(baseApi(read));
    renderHook(() => useActivityFeed());
    expect(read).toHaveBeenCalledWith("/activity");
  });
});
