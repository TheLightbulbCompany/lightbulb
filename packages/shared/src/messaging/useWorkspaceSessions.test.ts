// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LiveEvent } from "../live/types";
import { useWorkspaceSessions } from "./useWorkspaceSessions";
import type { RoomResourceSource } from "./useRoomResources";

const listWorkspaceSessions = vi.fn(async () => ({ sessions: [] }));
let handler: ((event: LiveEvent) => void) | null = null;
const source = {
  client: { listWorkspaceSessions, listResources: vi.fn(), listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
  onEvent: (h: (event: LiveEvent) => void) => { handler = h; return () => { handler = null; }; },
  connectionRevision: 1,
} as unknown as RoomResourceSource;

afterEach(() => { cleanup(); vi.useRealTimers(); listWorkspaceSessions.mockClear(); handler = null; });

describe("useWorkspaceSessions", () => {
  it("fetches once, polls every 30s, and stops when disabled", async () => {
    vi.useFakeTimers();
    const { rerender } = renderHook(({ enabled }) => useWorkspaceSessions(source, { enabled }), { initialProps: { enabled: true } });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(2);
    rerender({ enabled: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(2);
  });

  it("refetches on workspace.sessions.changed and ignores room hints", async () => {
    renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(1));
    act(() => handler?.({ eventId: "e1", type: "room.resources.changed", roomId: "r", payload: { roomId: "r" } } as unknown as LiveEvent));
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1);
    act(() => handler?.({ eventId: "e2", type: "workspace.sessions.changed", payload: { humanUserId: "alice" } } as unknown as LiveEvent));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(2));
  });

  it("keeps the last snapshot and reports an error when a refresh fails", async () => {
    listWorkspaceSessions.mockResolvedValueOnce({ sessions: [{ sessionId: "s1" } as never] }).mockRejectedValueOnce(new Error("boom"));
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.sessions).toHaveLength(1);
    expect(result.current.error).toBe("Could not refresh shared sessions.");
  });
});
