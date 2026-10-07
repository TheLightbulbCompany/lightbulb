import { describe, test, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

// Mock useTeamsApi BEFORE importing the hook.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: vi.fn(),
}));

import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
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

function installRead(
  perPath: Record<string, { data?: unknown; isLoading?: boolean; error?: Error }>,
) {
  mockUseTeamsApi.mockReturnValue({
    read: makeRead(perPath),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
}

describe("useInboxTriage", () => {
  test("isError is false and count is 0 when the read returns nothing", () => {
    installRead({});
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.isError).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.count).toBe(0);
  });

  test("isError is true when the runs read errors", () => {
    installRead({ "/inbox/runs": { error: new Error("boom") } });
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.isError).toBe(true);
  });

  test("isLoading reflects the runs read still loading", () => {
    installRead({ "/inbox/runs": { isLoading: true } });
    const { result } = renderHook(() => useInboxTriage(new Set()));
    expect(result.current.isLoading).toBe(true);
    expect(result.current.isError).toBe(false);
  });

  test("counts failed runs, filtering acknowledged", () => {
    installRead({
      "/inbox/runs": {
        data: {
          runs: [
            { id: "r1", status: "failed" },
            { id: "r2", status: "failed" },
            { id: "r3", status: "succeeded" },
          ],
        },
      },
    });
    const { result } = renderHook(() => useInboxTriage(new Set(["r2"])));
    expect(result.current.failedRuns.map((r) => r.id)).toEqual(["r1"]);
    expect(result.current.count).toBe(1);
    expect(result.current.isError).toBe(false);
  });
});
