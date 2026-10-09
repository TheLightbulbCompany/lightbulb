import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

const patch = vi.fn(async () => ({ id: "iss_a" }));
const mutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: vi.fn(), patch, del: vi.fn(), mutate }),
}));

import { useIssueMutations } from "../useIssueMutations";

describe("useIssueMutations.update", () => {
  it("revalidates other tasks' details, whose Blocking chips show this one", async () => {
    const { result } = renderHook(() => useIssueMutations());
    await result.current.update("iss_a", { blockedByIssueIds: ["iss_b"] });
    const filters = mutate.mock.calls.map(([k]) => k).filter((k) => typeof k === "function");
    const revalidated = (key: string) => filters.some((f) => f(key));
    expect(revalidated("/teams/issues/iss_b")).toBe(true);
    expect(revalidated("/teams/issues/iss_b/comments")).toBe(false);
  });
});
