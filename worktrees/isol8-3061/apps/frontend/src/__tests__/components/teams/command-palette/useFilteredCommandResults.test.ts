// Smoke tests for useFilteredCommandResults — enabled gate, full lists with no
// query, case-insensitive substring filtering on title/identifier, normalization
// of envelope shapes ({agents:[]}, {items:[]}), and 10-result cap per group.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { useFilteredCommandResults } from "@/components/teams/command-palette/useFilteredCommandResults";
import { useTeamsApi } from "@/hooks/useTeamsApi";

const mockUseTeamsApi = vi.mocked(useTeamsApi);

function makeRead(perPath: Record<string, unknown>) {
  return vi.fn((path: string) => ({
    data: perPath[path], isLoading: false, error: null, mutate: vi.fn(), isValidating: false,
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useFilteredCommandResults", () => {
  test("returns empty when enabled=false", () => {
    mockUseTeamsApi.mockReturnValue({
      read: makeRead({ "/agents": [{ id: "a1", name: "X" }], "/issues": [], "/projects": [] }),
      post: vi.fn(), patch: vi.fn(), del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
    const { result } = renderHook(() => useFilteredCommandResults("", false));
    expect(result.current.agents).toEqual([]);
  });

  test("returns full lists when enabled=true and no query", () => {
    mockUseTeamsApi.mockReturnValue({
      read: makeRead({
        "/agents": [{ id: "a1", name: "Main" }],
        "/issues": [{ id: "i1", title: "Bug", status: "todo" }],
        "/projects": [{ id: "p1", name: "Inbox" }],
      }),
      post: vi.fn(), patch: vi.fn(), del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
    const { result } = renderHook(() => useFilteredCommandResults("", true));
    expect(result.current.agents).toHaveLength(1);
    expect(result.current.issues).toHaveLength(1);
    expect(result.current.projects).toHaveLength(1);
  });

  test("filters issues by title substring (case-insensitive)", () => {
    mockUseTeamsApi.mockReturnValue({
      read: makeRead({
        "/agents": [],
        "/issues": [
          { id: "i1", title: "Fix the inbox", status: "todo" },
          { id: "i2", title: "Ship feature", status: "todo" },
        ],
        "/projects": [],
      }),
      post: vi.fn(), patch: vi.fn(), del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
    const { result } = renderHook(() => useFilteredCommandResults("INBOX", true));
    expect(result.current.issues.map((i) => i.id)).toEqual(["i1"]);
  });

  test("normalizes envelope shapes ({agents: []} or {items: []})", () => {
    mockUseTeamsApi.mockReturnValue({
      read: makeRead({
        "/agents": { agents: [{ id: "a1", name: "Foo" }] },
        "/issues": { items: [{ id: "i1", title: "X", status: "todo" }] },
        "/projects": [],
      }),
      post: vi.fn(), patch: vi.fn(), del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
    const { result } = renderHook(() => useFilteredCommandResults("", true));
    expect(result.current.agents).toHaveLength(1);
    expect(result.current.issues).toHaveLength(1);
  });

  test("limits each group to 10 results", () => {
    const issues = Array.from({ length: 25 }, (_, i) => ({ id: `i${i}`, title: "x", status: "todo" }));
    mockUseTeamsApi.mockReturnValue({
      read: makeRead({ "/agents": [], "/issues": issues, "/projects": [] }),
      post: vi.fn(), patch: vi.fn(), del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
    const { result } = renderHook(() => useFilteredCommandResults("", true));
    expect(result.current.issues).toHaveLength(10);
  });

  test("issues filter matches identifier too", () => {
    mockUseTeamsApi.mockReturnValue({
      read: makeRead({
        "/agents": [],
        "/issues": [
          { id: "i1", title: "Fix", identifier: "PAP-42", status: "todo" },
          { id: "i2", title: "Ship", identifier: "OTHER-1", status: "todo" },
        ],
        "/projects": [],
      }),
      post: vi.fn(), patch: vi.fn(), del: vi.fn(),
    } as unknown as ReturnType<typeof useTeamsApi>);
    const { result } = renderHook(() => useFilteredCommandResults("PAP-42", true));
    expect(result.current.issues.map((i) => i.id)).toEqual(["i1"]);
  });
});
