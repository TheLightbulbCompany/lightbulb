import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

const auth = vi.hoisted(() => ({ isLoaded: true, userId: "user-a", orgId: "org-a" as string | null }));
const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
import { useAssistantDm } from "../useAssistantDm";

beforeEach(() => { Object.assign(auth, { isLoaded: true, userId: "user-a", orgId: "org-a" }); api.get.mockReset(); });

it.each(["orgId", "userId"] as const)("never reuses the prior assistant DM after %s changes", async (field) => {
  let oldDone!: (value: unknown) => void;
  let newDone!: (value: unknown) => void;
  api.get.mockImplementationOnce(() => new Promise((resolve) => { oldDone = resolve; }))
    .mockImplementationOnce(() => new Promise((resolve) => { newDone = resolve; }));
  const cache = new Map();
  const wrapper = ({ children }: { children: React.ReactNode }) => <SWRConfig value={{ provider: () => cache }}>{children}</SWRConfig>;
  const { result, rerender } = renderHook(() => useAssistantDm(), { wrapper });
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
  auth[field] = "another-scope";
  rerender();
  expect(result.current).toBeUndefined();
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  await act(async () => { oldDone({ rooms: [{ roomId: "old-room", firstRun: true }] }); });
  expect(result.current).toBeUndefined();
  await act(async () => { newDone({ rooms: [{ roomId: "new-room", firstRun: true }] }); });
  await waitFor(() => expect(result.current?.roomId).toBe("new-room"));
});

it("does not preconnect through a DM lookup before a workspace is selected", () => {
  auth.orgId = null;
  const { result } = renderHook(() => useAssistantDm());
  expect(result.current).toBeUndefined();
  expect(api.get).not.toHaveBeenCalled();
});
