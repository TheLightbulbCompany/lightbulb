/**
 * F-RETRY: a queued send that fails to dispatch is marked failed and is
 * retryable.
 *
 * When the drain attempts a queued send and the dispatch throws (e.g. the
 * socket dropped between the run-ended signal and the send), the item stays
 * in the queue with sendState:"failed" + sendError. retryQueuedMessage(id)
 * re-attempts it. Mirrors upstream OpenClaw web UI's retryQueuedChatMessage
 * (upstream ui/src/ui/chat/chat-queue.ts).
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

describe("useAgentChat — F-RETRY", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("marks a queued send as failed when its drain dispatch throws", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // First send succeeds and becomes the active run.
    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    // Queue a follow-up while busy.
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Now the socket drops: the next sendChat throws when the drain fires.
    sendChat.mockImplementationOnce(() => {
      throw new Error("Gateway connection lost");
    });

    // Run ends → drain attempts the follow-up → dispatch throws.
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );

    // The item is NOT lost — it stays queued, marked failed, with the error.
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("follow-up");
    expect(result.current.queuedMessages[0].sendState).toBe("failed");
    expect(result.current.queuedMessages[0].sendError).toBeTruthy();
  });

  it("retryQueuedMessage re-attempts a failed item and dispatches it", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });

    sendChat.mockImplementationOnce(() => {
      throw new Error("Gateway connection lost");
    });
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    expect(result.current.queuedMessages[0].sendState).toBe("failed");
    const id = result.current.queuedMessages[0].id;
    const callsBefore = sendChat.mock.calls.length;

    // Retry — the socket is back, the send goes through.
    await act(async () => {
      result.current.retryQueuedMessage(id);
    });

    expect(sendChat.mock.calls.length).toBe(callsBefore + 1);
    expect(sendChat.mock.calls[callsBefore]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("retryQueuedMessage is a no-op for an unknown id", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      result.current.retryQueuedMessage("nope");
    });
    expect(sendChat).not.toHaveBeenCalled();
  });
});
