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
  test("create posts to /issues with snake_cased body", async () => {
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
      project_id: "proj_1",
      assignee_agent_id: "ag_1",
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

  test("update with all fields snake_cases project + assignee", async () => {
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
      project_id: "proj_1",
      assignee_agent_id: "ag_1",
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

  // Regression: Codex P1 on PR #556 — the BFF CreateIssueBody schema forbids
  // `status`, so even if a caller smuggles it in (TS would normally block this,
  // but a rogue `as IssueCreateInput` cast or backports could) the create body
  // must NEVER include it, or upstream returns 422.
  test("create body NEVER includes status (BFF whitelist)", async () => {
    mockPost.mockResolvedValue({ id: "iss_1", title: "x", status: "todo" });
    const { result } = renderHook(() => useIssueMutations());
    await act(async () => {
      // Cast to bypass the type narrowing — the test exists precisely because
      // we want to defend against runtime smuggling of the field.
      await result.current.create({ title: "x", status: "in_progress" } as never);
    });
    const callBody = mockPost.mock.calls[0][1] as Record<string, unknown>;
    expect(callBody).not.toHaveProperty("status");
    expect(callBody).toMatchObject({ title: "x" });
  });
});
