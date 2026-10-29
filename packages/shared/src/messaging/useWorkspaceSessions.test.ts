// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LiveEvent } from "../live/types";
import { useWorkspaceSessions } from "./useWorkspaceSessions";
import type { SessionSource } from "./useLocalSessionHistory";

const listWorkspaceSessions = vi.fn(async () => ({ sessions: [] }));
let handler: ((event: LiveEvent) => void) | null = null;
const getLocalSession = vi.fn();
const source = {
  client: { listWorkspaceSessions, getLocalSession, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
  onEvent: (h: (event: LiveEvent) => void) => { handler = h; return () => { handler = null; }; },
  connectionRevision: 1,
} as unknown as SessionSource;

afterEach(() => { cleanup(); vi.useRealTimers(); listWorkspaceSessions.mockClear(); getLocalSession.mockReset(); handler = null; });
const hint = (payload: object) => ({ eventId: "h", type: "workspace.sessions.changed", payload: { humanUserId: "alice", ...payload } } as unknown as LiveEvent);

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

  it("drops removed rows at once, before the next refresh", async () => {
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

  it("re-reads only the named session on a hint that names one, and patches it in", async () => {
    listWorkspaceSessions.mockResolvedValueOnce({ sessions: [{ sessionId: "a", status: "busy" }, { sessionId: "b", status: "idle" }] } as never);
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));
    getLocalSession.mockResolvedValueOnce({ sessionId: "a", status: "waiting-approval", pendingApproval: { approvalId: "p1" } });
    act(() => handler?.(hint({ sessionId: "a" })));
    await waitFor(() => expect(result.current.sessions[0].status).toBe("waiting-approval"));
    expect(getLocalSession).toHaveBeenCalledWith("a");
    expect(listWorkspaceSessions).toHaveBeenCalledTimes(1); // no whole-list refetch
    expect(result.current.sessions[1]).toEqual({ sessionId: "b", status: "idle" });
  });

  it("refetches the list for a session it does not list, a refused read, or an end", async () => {
    const { ApiError } = await import("../api/error");
    listWorkspaceSessions.mockResolvedValue({ sessions: [{ sessionId: "a", status: "busy" }] } as never);
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    act(() => handler?.(hint({ sessionId: "new" })));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(2));
    expect(getLocalSession).not.toHaveBeenCalled();
    getLocalSession.mockRejectedValueOnce(new ApiError(404, { code: "LOCAL_SESSION_NOT_FOUND" }, "gone"));
    act(() => handler?.(hint({ sessionId: "a" })));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(3));
    getLocalSession.mockResolvedValueOnce({ sessionId: "a", status: "ended" });
    act(() => handler?.(hint({ sessionId: "a" })));
    await waitFor(() => expect(listWorkspaceSessions).toHaveBeenCalledTimes(4));
  });

  it("keeps a patched row over an older list read that answers after it", async () => {
    listWorkspaceSessions.mockResolvedValueOnce({ sessions: [{ sessionId: "a", status: "busy" }] } as never);
    const { result } = renderHook(() => useWorkspaceSessions(source));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    let answer: (v: unknown) => void = () => {};
    listWorkspaceSessions.mockImplementationOnce(() => new Promise(r => { answer = r; }) as never);
    act(() => handler?.(hint({})));  // a list read starts, still with the old row
    getLocalSession.mockResolvedValueOnce({ sessionId: "a", status: "idle" });
    act(() => handler?.(hint({ sessionId: "a" })));
    await waitFor(() => expect(result.current.sessions[0].status).toBe("idle"));
    await act(async () => { answer({ sessions: [{ sessionId: "a", status: "busy" }] }); });
    expect(result.current.sessions[0].status).toBe("idle");
  });
});
