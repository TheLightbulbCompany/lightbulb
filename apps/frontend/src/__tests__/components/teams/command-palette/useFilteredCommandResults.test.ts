// Smoke tests for useFilteredCommandResults — enabled gate, the full list with no
// query, case-insensitive name filtering, envelope shapes ({agents:[]}, {items:[]}),
// and the 10-result cap.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { useFilteredCommandResults } from "@/components/teams/command-palette/useFilteredCommandResults";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUseTeamsApi = vi.mocked(useTeamsApi);

function withAgents(agents: unknown) {
  mockUseTeamsApi.mockReturnValue({
    read: vi.fn((path: string) => ({ data: path === "/agents" ? agents : undefined, isLoading: false, error: null, mutate: vi.fn(), isValidating: false })),
    post: vi.fn(), patch: vi.fn(), del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useFilteredCommandResults", () => {
  test("returns empty when enabled=false", () => {
    withAgents([{ id: "a1", name: "X" }]);
    expect(renderHook(() => useFilteredCommandResults("", false)).result.current.agents).toEqual([]);
  });

  test("filters agents by name (case-insensitive) and reads both envelope shapes", () => {
    withAgents({ agents: [{ id: "a1", name: "Finance" }, { id: "a2", name: "Growth" }] });
    expect(renderHook(() => useFilteredCommandResults("FIN", true)).result.current.agents.map((a) => a.id)).toEqual(["a1"]);
    withAgents({ items: [{ id: "a3", name: "Ops" }] });
    expect(renderHook(() => useFilteredCommandResults("", true)).result.current.agents).toHaveLength(1);
  });

  test("limits the group to 10 results", () => {
    withAgents(Array.from({ length: 25 }, (_, i) => ({ id: `a${i}`, name: "x" })));
    expect(renderHook(() => useFilteredCommandResults("", true)).result.current.agents).toHaveLength(10);
  });
});
