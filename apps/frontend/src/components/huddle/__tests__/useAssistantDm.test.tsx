import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

const auth = vi.hoisted(() => ({ isLoaded: true, userId: "user-a" as string | null }));
const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
import { useAssistantDm } from "../useAssistantDm";

beforeEach(() => { Object.assign(auth, { isLoaded: true, userId: "user-a" }); api.get.mockReset(); });

it("never reuses the prior assistant DM after the signed-in user changes", async () => {
  let oldDone!: (value: unknown) => void;
  let newDone!: (value: unknown) => void;
  api.get.mockImplementationOnce(() => new Promise((resolve) => { oldDone = resolve; }))
    .mockImplementationOnce(() => new Promise((resolve) => { newDone = resolve; }));
  const cache = new Map();
  const wrapper = ({ children }: { children: React.ReactNode }) => <SWRConfig value={{ provider: () => cache }}>{children}</SWRConfig>;
  const { result, rerender } = renderHook(() => useAssistantDm(), { wrapper });
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
  auth.userId = "another-user";
  rerender();
  expect(result.current).toBeUndefined();
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  await act(async () => { oldDone({ rooms: [{ roomId: "old-room", firstRun: true }] }); });
  expect(result.current).toBeUndefined();
  await act(async () => { newDone({ rooms: [{ roomId: "new-room", firstRun: true }] }); });
  await waitFor(() => expect(result.current?.roomId).toBe("new-room"));
});

it("test_assistant_dm_loads_without_org: a signed-in person with no organization gets their DM", async () => {
  api.get.mockResolvedValue({ rooms: [{ roomId: "dm-1", kind: "dm", members: [{ kind: "agent" }] }] });
  const { result } = renderHook(() => useAssistantDm());
  await waitFor(() => expect(result.current?.roomId).toBe("dm-1"));
  expect(api.get).toHaveBeenCalledWith("/messaging/rooms");
});

it("does not look up a DM before anyone is signed in", () => {
  auth.userId = null;
  const { result } = renderHook(() => useAssistantDm());
  expect(result.current).toBeUndefined();
  expect(api.get).not.toHaveBeenCalled();
});
