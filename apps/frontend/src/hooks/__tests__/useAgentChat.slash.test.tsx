/**
 * T7: local `/clear` & `/redirect`; cache-invalidation on `/reset|/compact|/new`.
 *
 * Two OpenClaw slash commands are control-ui-only (no server handler) and are
 * handled locally inside sendMessage:
 *   - `/clear`    → wipes the local transcript, sends NOTHING to the server.
 *   - `/redirect <msg>` → aborts the active run (the existing cancelMessage
 *                          path), then resends the remainder as a fresh message.
 *
 * Three more mutate server-side session state, so they DO reach the server, then
 * the local transcript cache is invalidated + history refetched so the displayed
 * messages can't diverge from the (reset/compacted) server session:
 *   - `/reset`, `/compact`, `/new`.
 *
 * Mirrors the useAgentChat.btw.test.tsx harness (renderHook + mocked useGateway).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(name: string, data: unknown) => void> = [];
const sendReq = vi.fn<(method: string, params?: unknown) => Promise<unknown>>();
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: (h: (name: string, data: unknown) => void) => {
      eventHandlers.push(h);
      return () => {
        eventHandlers = eventHandlers.filter((x) => x !== h);
      };
    },
    onChatMessage: (h: (msg: ChatIncomingMessage) => void) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

/** clientRunId minted for the most recent primary send (sendChat 3rd arg). */
function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

/** Count rendered chat message items (user + assistant), ignoring live pills. */
function messageCount(result: { current: { renderItems: unknown[] } }): number {
  return result.current.renderItems.filter(
    (i) => (i as { kind?: string }).kind === "message",
  ).length;
}

/** How many chat.history RPCs have fired so far (mount fetch + refetches). */
function historyFetchCount(): number {
  return sendReq.mock.calls.filter((c) => c[0] === "chat.history").length;
}

describe("useAgentChat — T7 slash handling", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("clears the transcript locally on /clear without sending to the server", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // Seed a real turn: user send + a streamed+folded assistant reply.
    await act(async () => {
      await result.current.sendMessage("hello");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "hi there",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );
    expect(messageCount(result)).toBeGreaterThan(0);

    // /clear wipes the transcript locally and sends NOTHING to the server.
    await act(async () => {
      await result.current.sendMessage("/clear");
    });

    expect(messageCount(result)).toBe(0);
    expect(result.current.isStreaming).toBe(false);
    // No new wire traffic: sendChat unchanged, raw send never used for /clear.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(send).not.toHaveBeenCalled();
  });

  it("/redirect aborts the active run and resends the remainder as a fresh message", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("work on task A");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "starting task A",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.sendMessage("/redirect work on task B instead");
    });

    // Aborted the active run (agent_abort carrying runA) via the cancel path...
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: "agent-X",
      runId: runA,
    });
    // ...then resent only the remainder as a fresh primary send.
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("work on task B instead");
  });

  it("/reset is sent to the server, then invalidates the cache + refetches history", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // Mount triggers exactly one chat.history fetch.
    expect(historyFetchCount()).toBe(1);

    await act(async () => {
      await result.current.sendMessage("/reset");
    });

    // /reset reached the server as a normal primary send...
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0]?.[1]).toBe("/reset");
    // ...and a fresh chat.history fetch was triggered to re-sync the transcript.
    expect(historyFetchCount()).toBe(2);
  });

  it("/reset typed mid-run aborts the active run first, then sends /reset and refetches history", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));
    expect(historyFetchCount()).toBe(1);

    // Start a real turn so a run is in flight.
    await act(async () => {
      await result.current.sendMessage("work on task A");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "starting task A",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    // /reset mid-run must abort the in-flight run FIRST (mirrors /redirect)...
    await act(async () => {
      await result.current.sendMessage("/reset");
    });

    // ...emitting an agent_abort carrying the in-flight runId,
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: "agent-X",
      runId: runA,
    });
    // ...still sending /reset to the server as a fresh primary send,
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("/reset");
    // ...and triggering a history refetch.
    expect(historyFetchCount()).toBe(2);
  });

  it("/compact and /new also reach the server then refetch history", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));
    expect(historyFetchCount()).toBe(1);

    await act(async () => {
      await result.current.sendMessage("/compact");
    });
    expect(sendChat).toHaveBeenLastCalledWith("agent-X", "/compact", expect.any(String));
    expect(historyFetchCount()).toBe(2);

    await act(async () => {
      await result.current.sendMessage("/new");
    });
    expect(sendChat).toHaveBeenLastCalledWith("agent-X", "/new", expect.any(String));
    expect(historyFetchCount()).toBe(3);
  });

  it("does not treat a plain message that merely contains 'clear' as a command", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("please clear my calendar");
    });

    // Ordinary send — through sendChat, transcript intact.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0]?.[1]).toBe("please clear my calendar");
    expect(messageCount(result)).toBeGreaterThan(0);
  });
});
