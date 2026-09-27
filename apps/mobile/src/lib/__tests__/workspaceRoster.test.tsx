import { act, renderHook } from "@testing-library/react-native";

const mockFetchAgents = jest.fn();
const mockFetchMembers = jest.fn();
const mockConfig = {};
jest.mock("../teamsData", () => ({
  useTeamsConfig: () => mockConfig,
  fetchAgents: (...a: unknown[]) => mockFetchAgents(...a),
  fetchMembers: (...a: unknown[]) => mockFetchMembers(...a),
}));

import { ROSTER_POLL_MS, useWorkspaceRoster } from "../workspaceRoster";

beforeEach(() => {
  jest.useFakeTimers();
  mockFetchAgents.mockReset();
  mockFetchMembers.mockReset();
  mockFetchMembers.mockResolvedValue([{ userId: "u1", name: "Leo" }]);
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
