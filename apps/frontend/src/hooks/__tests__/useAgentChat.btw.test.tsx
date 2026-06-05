/**
 * T8: `/btw` (and `/side`) detached side-channel sender for useAgentChat.
 *
 * A `/btw …` message is a non-blocking aside that runs ALONGSIDE the main
 * turn rather than queuing behind it. sendMessage detects the prefix and:
 *   - sends {type:"agent_chat", agent_id, message:<FULL text incl. prefix>,
 *     side:true} IMMEDIATELY, even while a primary run is active;
 *   - does NOT enqueue it;
 *   - does NOT touch clientRunIdRef / isStreaming (a side run is detached).
 *
 * Crucially, a side run's eventual `run_ended` must NOT drain the primary
 * queue: drainQueue is guarded by !isChatBusy (clientRunIdRef set during the
 * primary run) + runId dedupe, so a side run_ended while the primary is
 * active is a no-op. Mirrors upstream OpenClaw web UI's sendDetachedBtwMessage
 * (~/Desktop/openclaw/ui/src/ui/app-chat.ts:706).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(name: string, data: unknown) => void> = [];
const sendReq = vi.fn().mockResolvedValue({});
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

function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — T8 /btw side-channel", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset();
    vi.resetModules();
  });

  it("sends a /btw message as {side:true} with the prefix intact, not via sendChat", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("/btw check the logs while you work");
    });

    // No primary run started, nothing queued.
    expect(sendChat).not.toHaveBeenCalled();
    expect(result.current.queuedMessages).toHaveLength(0);
    expect(result.current.isStreaming).toBe(false);

    // Sent as a detached side run, FULL text incl. the /btw prefix.
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "agent_chat",
      agent_id: "agent-X",
      message: "/btw check the logs while you work",
      side: true,
    });
  });

  it("also recognizes the /side alias", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("/side peek at the queue");
    });

    expect(send).toHaveBeenCalledWith({
      type: "agent_chat",
      agent_id: "agent-X",
      message: "/side peek at the queue",
      side: true,
    });
    expect(sendChat).not.toHaveBeenCalled();
  });

  it("sends /btw IMMEDIATELY while a primary run is active — does NOT enqueue or disturb the primary run", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // Start a primary run.
    await act(async () => {
      await result.current.sendMessage("primary turn");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working...",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    // /btw while busy: dispatched as a side run, NOT queued.
    await act(async () => {
      await result.current.sendMessage("/btw quick aside");
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "agent_chat",
      agent_id: "agent-X",
      message: "/btw quick aside",
      side: true,
    });
    // The primary run is undisturbed: still the only sendChat, still streaming,
    // nothing in the queue.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(0);
    expect(result.current.isStreaming).toBe(true);
  });

  it("a side run_ended while the primary run is active does NOT drain the queue", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // Primary run in flight with a queued follow-up behind it.
    await act(async () => {
      await result.current.sendMessage("primary turn");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working...",
      } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("queued follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Fire a /btw side run; it carries no clientRunId and shouldn't queue.
    await act(async () => {
      await result.current.sendMessage("/btw side note");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // The side run ends with its OWN runId while the primary is still active.
    // The drain is guarded by !isChatBusy (clientRunIdRef === runA), so this
    // run_ended must NOT flush the queued follow-up.
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: "side-run-id",
        outcome: "done",
      } as ChatIncomingMessage),
    );

    // Still only the primary sendChat; the follow-up stays queued.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("queued follow-up");

    // When the PRIMARY run truly ends, the queue drains normally.
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: runA,
        outcome: "done",
      } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("queued follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("does not treat a non-prefix message containing 'btw' mid-text as a side run", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("by the way / btw is not a command here");
    });

    // Regular send — goes through sendChat, not the side channel.
    expect(send).not.toHaveBeenCalled();
    expect(sendChat).toHaveBeenCalledTimes(1);
  });
});
