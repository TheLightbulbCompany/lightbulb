import { describe, test, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, useSWRConfig: vi.fn() };
});

import { useGoalMutations } from "@/components/teams/goals/hooks/useGoalMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useSWRConfig } from "swr";

const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();
const mockMutate = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useTeamsApi).mockReturnValue({
    read: vi.fn(),
    post: mockPost,
    patch: mockPatch,
    del: mockDel,
  } as unknown as ReturnType<typeof useTeamsApi>);
  vi.mocked(useSWRConfig).mockReturnValue({
    cache: { get: vi.fn(), set: vi.fn(), delete: vi.fn(), keys: vi.fn() },
    mutate: mockMutate,
  } as unknown as ReturnType<typeof useSWRConfig>);
});

describe("useGoalMutations", () => {
  test("create posts to /goals with snake_case body + invalidates list key", async () => {
    mockPost.mockResolvedValue({ id: "g_1", title: "Ship", status: "active", level: "task" });
    const { result } = renderHook(() => useGoalMutations());
    let created;
    await act(async () => {
      created = await result.current.create({
        title: "Ship",
        description: "details",
        parent_id: "g_0",
      });
    });
    expect(mockPost).toHaveBeenCalledWith("/goals", {
      title: "Ship",
      description: "details",
      parent_id: "g_0",
    });
    expect(created).toMatchObject({ id: "g_1" });
    expect(mockMutate).toHaveBeenCalledWith("/teams/goals");
  });

  test("update patches /goals/{id} + invalidates detail + list keys", async () => {
    mockPatch.mockResolvedValue({ id: "g_1", title: "x", status: "achieved", level: "task" });
    const { result } = renderHook(() => useGoalMutations());
    await act(async () => {
      await result.current.update("g_1", { status: "achieved" });
    });
    expect(mockPatch).toHaveBeenCalledWith("/goals/g_1", { status: "achieved" });
    // 2 mutate calls: detail key + list key
    expect(mockMutate).toHaveBeenCalledTimes(2);
    expect(mockMutate).toHaveBeenCalledWith("/teams/goals/g_1");
    expect(mockMutate).toHaveBeenCalledWith("/teams/goals");
  });

  test("update forwards level (the Level picker)", async () => {
    mockPatch.mockResolvedValue({ id: "g_1", title: "x", status: "active", level: "team" });
    const { result } = renderHook(() => useGoalMutations());
    await act(async () => {
      await result.current.update("g_1", { level: "team" });
    });
    expect(mockPatch).toHaveBeenCalledWith("/goals/g_1", { level: "team" });
  });

  test("remove deletes /goals/{id} + invalidates list key", async () => {
    mockDel.mockResolvedValue(undefined);
    const { result } = renderHook(() => useGoalMutations());
    await act(async () => {
      await result.current.remove("g_1");
    });
    expect(mockDel).toHaveBeenCalledWith("/goals/g_1");
    // Single mutate() invalidating the list (detail key intentionally skipped).
    expect(mockMutate).toHaveBeenCalledTimes(1);
    expect(mockMutate).toHaveBeenCalledWith("/teams/goals");
  });

  test("create error rejects + does NOT invalidate cache", async () => {
    mockPost.mockRejectedValue(new Error("server boom"));
    const { result } = renderHook(() => useGoalMutations());
    await act(async () => {
      await expect(result.current.create({ title: "x" })).rejects.toThrow("server boom");
    });
    expect(mockMutate).not.toHaveBeenCalled();
  });
});
