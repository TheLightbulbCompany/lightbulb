import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { useGoalDetail } from "@/components/teams/goals/hooks/useGoalDetail";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUseTeamsApi = vi.mocked(useTeamsApi);

function makeRead(
  perPath: Record<string, { data?: unknown; isLoading?: boolean; error?: Error }>,
) {
  return vi.fn((path: string) => {
    const r = perPath[path] ?? { data: undefined, isLoading: false, error: null };
    return {
      data: r.data,
      isLoading: r.isLoading ?? false,
      error: r.error ?? null,
      mutate: vi.fn(),
      isValidating: false,
    };
  });
}

const baseApi = (read: ReturnType<typeof makeRead>) =>
  ({
    read,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }) as unknown as ReturnType<typeof useTeamsApi>;

describe("useGoalDetail", () => {
  test("returns undefined goal + empty lists when reads return undefined", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({})));
    const { result } = renderHook(() => useGoalDetail("g_1"));
    expect(result.current.goal).toBeUndefined();
    expect(result.current.allGoals).toEqual([]);
    expect(result.current.projects).toEqual([]);
    expect(result.current.isLoading).toBe(false);
  });

  test("returns goal + normalized goal list envelope + projects envelope", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/goals/g_1": {
            data: { id: "g_1", title: "Ship v1", status: "active", level: "company" },
          },
          "/goals": {
            data: { goals: [{ id: "g_1", title: "Ship v1", status: "active", level: "company" }] },
          },
          "/projects": { data: { projects: [{ id: "p_1", name: "Proj" }] } },
        }),
      ),
    );
    const { result } = renderHook(() => useGoalDetail("g_1"));
    expect(result.current.goal?.id).toBe("g_1");
    expect(result.current.allGoals).toHaveLength(1);
    expect(result.current.projects).toHaveLength(1);
  });

  test("normalizes goal list + projects returned as bare arrays", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/goals/g_1": { data: { id: "g_1", title: "x", status: "active", level: "task" } },
          "/goals": { data: [{ id: "g_1", title: "x", status: "active", level: "task" }] },
          "/projects": { data: [{ id: "p_1", name: "Proj" }] },
        }),
      ),
    );
    const { result } = renderHook(() => useGoalDetail("g_1"));
    expect(result.current.allGoals).toHaveLength(1);
    expect(result.current.projects).toHaveLength(1);
  });

  test("isLoading true when any read is loading", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ "/goals/g_1": { isLoading: true } })),
    );
    const { result } = renderHook(() => useGoalDetail("g_1"));
    expect(result.current.isLoading).toBe(true);
  });

  test("isError true + surfaces error when goal read errors", () => {
    const err = new Error("boom");
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ "/goals/g_1": { error: err } })),
    );
    const { result } = renderHook(() => useGoalDetail("g_1"));
    expect(result.current.isError).toBe(true);
    expect(result.current.error).toBe(err);
  });

  test("a projects-list error does NOT flip isError (only the goal read gates it)", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/goals/g_1": { data: { id: "g_1", title: "x", status: "active", level: "task" } },
          "/projects": { error: new Error("projects down") },
        }),
      ),
    );
    const { result } = renderHook(() => useGoalDetail("g_1"));
    expect(result.current.isError).toBe(false);
    expect(result.current.goal?.id).toBe("g_1");
  });

  test("calls read with the queryKeys-derived paths", () => {
    const read = makeRead({});
    mockUseTeamsApi.mockReturnValue(baseApi(read));
    renderHook(() => useGoalDetail("g_1"));
    expect(read).toHaveBeenCalledWith("/goals/g_1");
    expect(read).toHaveBeenCalledWith("/goals");
    expect(read).toHaveBeenCalledWith("/projects");
  });
});
