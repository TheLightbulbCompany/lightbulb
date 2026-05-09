import { describe, test, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, useSWRConfig: vi.fn() };
});

import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useSWRConfig } from "swr";

const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockMutate = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useTeamsApi).mockReturnValue({
    read: vi.fn(),
    post: mockPost,
    patch: mockPatch,
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
  vi.mocked(useSWRConfig).mockReturnValue({
    cache: { get: vi.fn(), set: vi.fn(), delete: vi.fn(), keys: vi.fn() },
    mutate: mockMutate,
  } as unknown as ReturnType<typeof useSWRConfig>);
});

describe("useIssueMutations", () => {
  test("create posts to /issues with camelCase body", async () => {
    mockPost.mockResolvedValue({ id: "iss_1", title: "Fix bug", status: "todo" });
    const { result } = renderHook(() => useIssueMutations());
    let created;
    await act(async () => {
      created = await result.current.create({
        title: "Fix bug",
        description: "details",
        priority: "high",
        projectId: "proj_1",
        assigneeAgentId: "ag_1",
      });
    });
    expect(mockPost).toHaveBeenCalledWith("/issues", {
      title: "Fix bug",
      description: "details",
      priority: "high",
      projectId: "proj_1",
      assigneeAgentId: "ag_1",
    });
    expect(created).toMatchObject({ id: "iss_1" });
  });

  test("create invalidates inbox keys via predicate", async () => {
    mockPost.mockResolvedValue({ id: "iss_1", title: "x", status: "todo" });
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => { await result.current.create({ title: "x" }); });
    // Predicate-based invalidation = single mutate() call with a function arg
    expect(mockMutate).toHaveBeenCalledTimes(1);
    const arg = mockMutate.mock.calls[0][0];
    expect(typeof arg).toBe("function");
    // Sanity: the predicate matches inbox keys
    expect((arg as (k: unknown) => boolean)("/teams/inbox?tab=mine")).toBe(true);
    expect((arg as (k: unknown) => boolean)("/teams/issues/iss_1")).toBe(false);
  });

  test("update patches /issues/{id} + invalidates detail + inbox keys", async () => {
    mockPatch.mockResolvedValue({ id: "iss_1", title: "Fix", status: "in_progress" });
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => {
      await result.current.update("iss_1", { status: "in_progress" });
    });
    expect(mockPatch).toHaveBeenCalledWith("/issues/iss_1", { status: "in_progress" });
    // 2 mutate calls: detail key + inbox predicate
    expect(mockMutate).toHaveBeenCalledTimes(2);
    expect(mockMutate).toHaveBeenCalledWith("/teams/issues/iss_1");
  });

  test("addComment posts body + invalidates comments key", async () => {
    mockPost.mockResolvedValue({ id: "c1", body: "Hi", createdAt: "2026-05-05T00:00:00Z" });
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => {
      await result.current.addComment("iss_1", "Hi");
    });
    expect(mockPost).toHaveBeenCalledWith("/issues/iss_1/comments", { body: "Hi" });
    expect(mockMutate).toHaveBeenCalledWith("/teams/issues/iss_1/comments");
  });

  test("update with all fields forwards camelCase project + assignee", async () => {
    mockPatch.mockResolvedValue({ id: "iss_1", title: "x", status: "todo" });
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => {
      await result.current.update("iss_1", {
        title: "x",
        description: "y",
        status: "todo",
        priority: "low",
        projectId: "proj_1",
        assigneeAgentId: "ag_1",
      });
    });
    expect(mockPatch).toHaveBeenCalledWith("/issues/iss_1", {
      title: "x",
      description: "y",
      status: "todo",
      priority: "low",
      projectId: "proj_1",
      assigneeAgentId: "ag_1",
    });
  });

  test("create error rejects + does NOT invalidate cache", async () => {
    mockPost.mockRejectedValue(new Error("server boom"));
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => {
      await expect(result.current.create({ title: "x" })).rejects.toThrow("server boom");
    });
    expect(mockMutate).not.toHaveBeenCalled();
  });

  // Whitelist enforcement: since the camelCase unification (Task 3 of
  // 2026-05-09-teams-bff-camelcase-unification), the frontend forwards
  // IssueCreateInput verbatim to the BFF — there is no client-side body
  // builder stripping disallowed fields. The BFF's CreateIssueBody schema
  // (extra="forbid") is the single source of truth for the whitelist; if a
  // caller smuggles `status` past TypeScript via a `as never` cast, the
  // BFF will 422. This test pins the new contract: the body the frontend
  // posts is exactly the (typed) input.
  test("create posts the typed input verbatim (whitelist enforced server-side)", async () => {
    mockPost.mockResolvedValue({ id: "iss_1", title: "x", status: "todo" });
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => {
      await result.current.create({ title: "x", priority: "low" });
    });
    expect(mockPost).toHaveBeenCalledWith("/issues", { title: "x", priority: "low" });
  });
});
