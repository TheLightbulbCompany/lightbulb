import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { useProjectDetail } from "@/components/teams/projects/hooks/useProjectDetail";
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

describe("useProjectDetail", () => {
  test("returns undefined project + empty issues when reads return undefined", () => {
    mockUseTeamsApi.mockReturnValue(baseApi(makeRead({})));
    const { result } = renderHook(() => useProjectDetail("p_1"));
    expect(result.current.project).toBeUndefined();
    expect(result.current.issues).toEqual([]);
    expect(result.current.isLoading).toBe(false);
  });

  test("returns project + normalized issues envelope (the Issues tab)", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/p_1": {
            data: { id: "p_1", name: "Marketing" },
          },
          "/issues?project=p_1": {
            data: { issues: [{ id: "i_1", title: "Task", status: "todo" }] },
          },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("p_1"));
    expect(result.current.project?.id).toBe("p_1");
    expect(result.current.project?.name).toBe("Marketing");
    expect(result.current.issues).toHaveLength(1);
  });

  test("normalizes issues returned as a bare array", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/p_1": { data: { id: "p_1", name: "x" } },
          "/issues?project=p_1": {
            data: [{ id: "i_1", title: "x", status: "todo" }],
          },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("p_1"));
    expect(result.current.issues).toHaveLength(1);
  });

  test("isLoading reflects the project read; issuesLoading is separate", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/p_1": { isLoading: true },
          "/issues?project=p_1": { isLoading: false },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("p_1"));
    expect(result.current.isLoading).toBe(true);
    expect(result.current.issuesLoading).toBe(false);
  });

  test("isError true + surfaces error when project read errors", () => {
    const err = new Error("boom");
    mockUseTeamsApi.mockReturnValue(
      baseApi(makeRead({ "/projects/p_1": { error: err } })),
    );
    const { result } = renderHook(() => useProjectDetail("p_1"));
    expect(result.current.isError).toBe(true);
    expect(result.current.error).toBe(err);
  });

  test("an issues-list error does NOT flip isError (only the project read gates it)", () => {
    mockUseTeamsApi.mockReturnValue(
      baseApi(
        makeRead({
          "/projects/p_1": { data: { id: "p_1", name: "x" } },
          "/issues?project=p_1": { error: new Error("issues down") },
        }),
      ),
    );
    const { result } = renderHook(() => useProjectDetail("p_1"));
    expect(result.current.isError).toBe(false);
    expect(result.current.project?.id).toBe("p_1");
  });

  test("calls read with the queryKeys-derived paths", () => {
    const read = makeRead({});
    mockUseTeamsApi.mockReturnValue(baseApi(read));
    renderHook(() => useProjectDetail("p_1"));
    expect(read).toHaveBeenCalledWith("/projects/p_1");
    expect(read).toHaveBeenCalledWith("/issues?project=p_1");
  });
});
