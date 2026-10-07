import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { useIssueDetail } from "@/components/teams/issues/hooks/useIssueDetail";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUseTeamsApi = vi.mocked(useTeamsApi);

function makeRead(perPath: Record<string, { data?: unknown; isLoading?: boolean; error?: Error }>) {
  return vi.fn((path: string) => {
    const r = perPath[path] ?? { data: undefined, isLoading: false, error: null };
    return { data: r.data, isLoading: r.isLoading ?? false, error: r.error ?? null, mutate: vi.fn(), isValidating: false };
  });
}

const baseApi = (read: ReturnType<typeof makeRead>) => ({
  read,
  post: vi.fn(),
  patch: vi.fn(),
  del: vi.fn(),
}) as unknown as ReturnType<typeof useTeamsApi>;

describe("useIssueDetail", () => {
  test("returns undefined issue + empty comments when reads return undefined", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({})));
    const { result } = renderHook(() => useIssueDetail("iss_1"));
    expect(result.current.issue).toBeUndefined();
    expect(result.current.comments).toEqual([]);
    expect(result.current.isLoading).toBe(false);
  });

  test("returns issue + normalized comments envelope", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({
      "/issues/iss_1": { data: { id: "iss_1", title: "Test", status: "todo" } },
      "/issues/iss_1/comments": { data: { comments: [{ id: "c1", body: "Hi", createdAt: "2026-05-05T00:00:00Z" }] } },
    })));
    const { result } = renderHook(() => useIssueDetail("iss_1"));
    expect(result.current.issue?.id).toBe("iss_1");
    expect(result.current.comments).toHaveLength(1);
    expect(result.current.comments[0].body).toBe("Hi");
  });

  test("normalizes comments returned as bare array", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({
      "/issues/iss_1": { data: { id: "iss_1", title: "Test", status: "todo" } },
      "/issues/iss_1/comments": { data: [{ id: "c1", body: "Hi", createdAt: "2026-05-05T00:00:00Z" }] },
    })));
    const { result } = renderHook(() => useIssueDetail("iss_1"));
    expect(result.current.comments).toHaveLength(1);
  });

  test("isLoading true when any read is loading", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({
      "/issues/iss_1": { isLoading: true },
    })));
    const { result } = renderHook(() => useIssueDetail("iss_1"));
    expect(result.current.isLoading).toBe(true);
  });

  test("isError true + surfaces error when issue read errors", () => {
    const err = new Error("boom");
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({
      "/issues/iss_1": { error: err },
    })));
    const { result } = renderHook(() => useIssueDetail("iss_1"));
    expect(result.current.isError).toBe(true);
    expect(result.current.error).toBe(err);
  });

  test("calls read with the queryKeys-derived paths", () => {
    const read = makeRead({});
    mockUseTeamsApi.mockReturnValue(baseApi(read));
    renderHook(() => useIssueDetail("iss_1"));
    expect(read).toHaveBeenCalledWith("/issues/iss_1");
    expect(read).toHaveBeenCalledWith("/issues/iss_1/comments");
  });
});
