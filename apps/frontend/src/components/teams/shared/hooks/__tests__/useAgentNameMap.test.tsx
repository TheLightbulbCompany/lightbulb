// The map that stopped the Activity page rendering raw ids. The behaviours
// worth pinning are the degenerate ones: the empty case has to keep a stable
// identity (consumers memoize on it), and a nameless agent must fall through
// to the caller's id fallback rather than resolving to an empty string, which
// would render as a gap where a name should be.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const h = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: h.read }) }));

import { useAgentNameMap } from "../useAgentNameMap";

beforeEach(() => h.read.mockReset());

describe("useAgentNameMap", () => {
  it("maps every named agent to its name", () => {
    h.read.mockReturnValue({
      data: { agents: [{ id: "ag_1", name: "Forge" }, { id: "ag_2", name: "Vera" }] },
    });
    const { result } = renderHook(() => useAgentNameMap());
    expect(result.current.get("ag_1")).toBe("Forge");
    expect(result.current.get("ag_2")).toBe("Vera");
  });

  it("keeps a stable identity while the read is in flight", () => {
    // A new Map every render would invalidate any consumer memoizing on it,
    // which is the whole reason the empty case is a module constant.
    h.read.mockReturnValue({ data: undefined });
    const { result, rerender } = renderHook(() => useAgentNameMap());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
    expect(first.size).toBe(0);
  });

  it("omits an agent with a blank name instead of mapping it to an empty string", () => {
    // Resolving to "" is worse than the id fallback: the row renders a gap
    // where a name should be, and nothing tells the reader who acted.
    h.read.mockReturnValue({
      data: { agents: [{ id: "ag_1", name: "" }, { id: "ag_2", name: "Vera" }] },
    });
    const { result } = renderHook(() => useAgentNameMap());
    expect(result.current.has("ag_1")).toBe(false);
    expect(result.current.get("ag_2")).toBe("Vera");
  });

  it("survives an empty roster", () => {
    h.read.mockReturnValue({ data: { agents: [] } });
    const { result } = renderHook(() => useAgentNameMap());
    expect(result.current.size).toBe(0);
  });

  it("reads the shared /agents path, so SWR dedupes it against sibling panels", () => {
    h.read.mockReturnValue({ data: { agents: [] } });
    renderHook(() => useAgentNameMap());
    expect(h.read).toHaveBeenCalledWith("/agents");
  });
});
