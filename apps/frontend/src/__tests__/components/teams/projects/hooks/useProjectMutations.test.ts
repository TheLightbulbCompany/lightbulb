import { describe, test, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, useSWRConfig: vi.fn() };
});

import { useProjectMutations } from "@/components/teams/projects/hooks/useProjectMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useSWRConfig } from "swr";

const mockPatch = vi.fn();
const mockMutate = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useTeamsApi).mockReturnValue({
    read: vi.fn(),
    post: vi.fn(),
    patch: mockPatch,
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
  vi.mocked(useSWRConfig).mockReturnValue({
    cache: { get: vi.fn(), set: vi.fn(), delete: vi.fn(), keys: vi.fn() },
    mutate: mockMutate,
  } as unknown as ReturnType<typeof useSWRConfig>);
});

describe("useProjectMutations", () => {
  test("update patches /projects/{id} + invalidates detail + list keys", async () => {
    mockPatch.mockResolvedValue({ id: "p_1", name: "Renamed" });
    const { result } = renderHook(() => useProjectMutations());
    let updated;
    await act(async () => {
      updated = await result.current.update("p_1", { name: "Renamed" });
    });
    expect(mockPatch).toHaveBeenCalledWith("/projects/p_1", { name: "Renamed" });
    expect(updated).toMatchObject({ id: "p_1" });
    // 2 mutate calls: detail key + list key.
    expect(mockMutate).toHaveBeenCalledTimes(2);
    expect(mockMutate).toHaveBeenCalledWith("/teams/projects/p_1");
    expect(mockMutate).toHaveBeenCalledWith("/teams/projects");
  });

  test("update forwards a null description (clearing the field)", async () => {
    mockPatch.mockResolvedValue({ id: "p_1", name: "x", description: null });
    const { result } = renderHook(() => useProjectMutations());
    await act(async () => {
      await result.current.update("p_1", { description: null });
    });
    expect(mockPatch).toHaveBeenCalledWith("/projects/p_1", {
      description: null,
    });
  });

  test("update patches a lifecycle status ({ status })", async () => {
    mockPatch.mockResolvedValue({ id: "p_1", status: "in_progress" });
    const { result } = renderHook(() => useProjectMutations());
    await act(async () => {
      await result.current.update("p_1", { status: "in_progress" });
    });
    expect(mockPatch).toHaveBeenCalledWith("/projects/p_1", {
      status: "in_progress",
    });
    // detail key + list key revalidated.
    expect(mockMutate).toHaveBeenCalledTimes(2);
  });

  test("update forwards goal_ids (replace-semantics link set)", async () => {
    mockPatch.mockResolvedValue({ id: "p_1" });
    const { result } = renderHook(() => useProjectMutations());
    await act(async () => {
      await result.current.update("p_1", { goal_ids: ["g_1", "g_2"] });
    });
    expect(mockPatch).toHaveBeenCalledWith("/projects/p_1", {
      goal_ids: ["g_1", "g_2"],
    });
  });

  test("update error rejects + does NOT invalidate cache", async () => {
    mockPatch.mockRejectedValue(new Error("server boom"));
    const { result } = renderHook(() => useProjectMutations());
    await act(async () => {
      await expect(
        result.current.update("p_1", { name: "x" }),
      ).rejects.toThrow("server boom");
    });
    expect(mockMutate).not.toHaveBeenCalled();
  });
});
