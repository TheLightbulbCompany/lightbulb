// apps/frontend/src/__tests__/teams/useAgentWake.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

// Annotate the mocked post so tsc (which type-checks test files) keeps the
// real generic signature; vitest strips types at runtime.
const post = vi.fn<(path: string, body: unknown) => Promise<unknown>>();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post }) }));

import { useAgentWake } from "@/components/teams/inbox/hooks/useAgentWake";

beforeEach(() => {
  post.mockReset().mockResolvedValue({});
});

describe("useAgentWake", () => {
  it("posts an on-demand wake with the retry reason to the agent's wakeup route", async () => {
    const { result } = renderHook(() => useAgentWake());
    await act(async () => {
      await result.current.wakeAgent("ag1", { reason: "retry" });
    });
    expect(post).toHaveBeenCalledWith("/agents/ag1/wakeup", {
      source: "on_demand",
      reason: "retry",
    });
  });

  it("defaults the reason to 'retry' when none is given", async () => {
    const { result } = renderHook(() => useAgentWake());
    await act(async () => {
      await result.current.wakeAgent("ag2");
    });
    expect(post).toHaveBeenCalledWith("/agents/ag2/wakeup", {
      source: "on_demand",
      reason: "retry",
    });
  });
});
