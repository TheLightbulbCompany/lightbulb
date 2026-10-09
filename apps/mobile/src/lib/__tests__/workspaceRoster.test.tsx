import { act, renderHook } from "@testing-library/react-native";

const mockFetchAgents = jest.fn();
const mockFetchMembers = jest.fn();
const mockConfig = {};
let mockOrgId: string | null = "org_1";
jest.mock("../teamsData", () => ({
  useTeamsConfig: () => mockConfig,
  fetchAgents: (...a: unknown[]) => mockFetchAgents(...a),
  fetchMembers: (...a: unknown[]) => mockFetchMembers(...a),
}));
jest.mock("@clerk/expo", () => ({
  useOrganization: () => ({ organization: mockOrgId ? { id: mockOrgId } : null }),
}));

import { ROSTER_POLL_MS, useWorkspaceRoster } from "../workspaceRoster";

beforeEach(() => {
  jest.useFakeTimers();
  mockFetchAgents.mockReset();
  mockFetchMembers.mockReset();
  mockFetchMembers.mockResolvedValue([{ userId: "u1", name: "Leo" }]);
  mockOrgId = "org_1";
});
afterEach(() => jest.useRealTimers());

it("loads agents with status and members, then re-polls agents", async () => {
  mockFetchAgents.mockResolvedValueOnce([{ id: "ceo", name: "CEO", status: "idle", seeded: true }])
    .mockResolvedValueOnce([{ id: "ceo", name: "CEO", status: "running", seeded: true }]);
  const { result } = await renderHook(() => useWorkspaceRoster(true));
  await act(async () => {});
  expect(result.current.agents[0].status).toBe("idle");
  expect(result.current.members).toEqual([{ userId: "u1", name: "Leo" }]);
  await act(async () => { jest.advanceTimersByTime(ROSTER_POLL_MS); });
  expect(result.current.agents[0].status).toBe("running");
});

it("keeps the last roster when a poll comes back empty", async () => {
  mockFetchAgents.mockResolvedValueOnce([{ id: "ceo", name: "CEO" }]).mockResolvedValueOnce([]);
  const { result } = await renderHook(() => useWorkspaceRoster(true));
  await act(async () => {});
  await act(async () => { jest.advanceTimersByTime(ROSTER_POLL_MS); });
  expect(result.current.agents).toHaveLength(1);
});

it("does nothing while disabled", async () => {
  await renderHook(() => useWorkspaceRoster(false));
  expect(mockFetchAgents).not.toHaveBeenCalled();
});

it("clears the old roster and refetches both agents and members when the org changes", async () => {
  mockFetchAgents.mockResolvedValueOnce([{ id: "ceo", name: "CEO" }]);
  mockFetchMembers.mockResolvedValueOnce([{ userId: "u1", name: "Leo" }]);
  const { result, rerender } = await renderHook(() => useWorkspaceRoster(true));
  await act(async () => {});
  expect(result.current.agents).toEqual([{ id: "ceo", name: "CEO" }]);
  expect(result.current.members).toEqual([{ userId: "u1", name: "Leo" }]);

  mockOrgId = "org_2";
  // Hold the new org's fetches open so the intermediate (post-switch,
  // pre-response) state is actually observable, not raced past by the
  // resolved-promise microtask that `rerender` also flushes.
  let resolveAgents: (v: unknown) => void = () => {};
  let resolveMembers: (v: unknown) => void = () => {};
  mockFetchAgents.mockImplementationOnce(() => new Promise((resolve) => { resolveAgents = resolve; }));
  mockFetchMembers.mockImplementationOnce(() => new Promise((resolve) => { resolveMembers = resolve; }));
  await rerender({});
  // The old workspace's roster must not linger after the switch, even before
  // the new org's fetch has resolved.
  expect(result.current.agents).toEqual([]);
  expect(result.current.members).toEqual([]);
  expect(mockFetchAgents).toHaveBeenCalledTimes(2);
  expect(mockFetchMembers).toHaveBeenCalledTimes(2);

  await act(async () => {
    resolveAgents([{ id: "cfo", name: "CFO" }]);
    resolveMembers([{ userId: "u2", name: "Ann" }]);
  });
  expect(result.current.agents).toEqual([{ id: "cfo", name: "CFO" }]);
  expect(result.current.members).toEqual([{ userId: "u2", name: "Ann" }]);
});
