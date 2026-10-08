// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LiveEvent } from "../live/types";
import { useWorkspaceSessions } from "./useWorkspaceSessions";
import type { SessionSource } from "./useLocalSessionHistory";

const listWorkspaceSessions = vi.fn(async () => ({ sessions: [] }));
let handler: ((event: LiveEvent) => void) | null = null;
const source = {
  client: { listWorkspaceSessions, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
  onEvent: (h: (event: LiveEvent) => void) => { handler = h; return () => { handler = null; }; },
  connectionRevision: 1,
} as unknown as SessionSource;

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

  it("forgets removed rows at once, before the next refresh", async () => {
    listWorkspaceSessions.mockResolvedValueOnce({ sessions: [{ sessionId: "a" }, { sessionId: "b" }] } as never);
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    act(() => result.current.forget((session) => session.sessionId === "a"));
    expect(result.current.sessions.map((session) => session.sessionId)).toEqual(["b"]);
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1);
  });

  it("refetches on workspace.sessions.changed and ignores other events", async () => {
    renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(1));
    act(() => handler?.({ eventId: "e1", type: "room.metadata.changed", roomId: "r", payload: { roomId: "r" } } as unknown as LiveEvent));
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1);
    act(() => handler?.({ eventId: "e2", type: "workspace.sessions.changed", payload: { humanUserId: "alice" } } as unknown as LiveEvent));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(2));
    // A streaming batch changes an open session's content, not the list.
    act(() => handler?.({ eventId: "e3", type: "workspace.sessions.changed", payload: { humanUserId: "alice", transcript: true } } as unknown as LiveEvent));
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(2);
  });

  it("refetches when a message lands in a listed session's chat, so its unread count is the server's", async () => {
    listWorkspaceSessions.mockResolvedValueOnce({ sessions: [{ sessionId: "s1", chatRoomId: "chat-1" } as never] });
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    const available = (payload: object) => ({ eventId: "e", type: "room.message.available", payload } as unknown as LiveEvent);
    act(() => handler?.(available({ roomId: "other", messageSeq: 3 })));
    act(() => handler?.(available({ roomId: "chat-1", messageSeq: 3, previewOnly: true })));
    act(() => handler?.(available({ roomId: "chat-1", messageSeq: 3, threadRootMessageId: "m1" })));
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1);
    act(() => handler?.(available({ roomId: "chat-1", messageSeq: 3 })));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(2));
  });

  it("bumps hintRevision on every sessions hint, transcript ones included", async () => {
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(1));
    expect(result.current.hintRevision).toBe(0);
    act(() => handler?.({ eventId: "e1", type: "workspace.sessions.changed", payload: { humanUserId: "alice", transcript: true } } as unknown as LiveEvent));
    act(() => handler?.({ eventId: "e2", type: "workspace.sessions.changed", payload: { humanUserId: "alice" } } as unknown as LiveEvent));
    expect(result.current.hintRevision).toBe(2);
    act(() => handler?.({ eventId: "e3", type: "room.metadata.changed", roomId: "r", payload: { roomId: "r" } } as unknown as LiveEvent));
    expect(result.current.hintRevision).toBe(2);
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
