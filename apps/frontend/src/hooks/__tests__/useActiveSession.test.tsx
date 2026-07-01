import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

describe("useActiveSession — shared per-agent session store", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("defaults to the General session (null) for an agent", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const { result } = renderHook(() => useActiveSession("agent-X"));
    expect(result.current.activeSessionId).toBeNull();
  });

  it("newSession() mints a lowercase UUIDv4 and sets it active", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const { result } = renderHook(() => useActiveSession("agent-X"));
    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });
    expect(minted).toMatch(UUID_RE);
    expect(result.current.activeSessionId).toBe(minted);
  });

  it("two hooks on the same agent see the same value (cross-subtree sharing)", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const a = renderHook(() => useActiveSession("agent-X"));
    const b = renderHook(() => useActiveSession("agent-X"));
    act(() => {
      a.result.current.selectSession("11111111-1111-4111-8111-111111111111");
    });
    expect(b.result.current.activeSessionId).toBe(
      "11111111-1111-4111-8111-111111111111",
    );
  });

  it("keeps per-agent state independent", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const x = renderHook(() => useActiveSession("agent-X"));
    const y = renderHook(() => useActiveSession("agent-Y"));
    act(() => x.result.current.newSession());
    expect(y.result.current.activeSessionId).toBeNull();
  });
});
