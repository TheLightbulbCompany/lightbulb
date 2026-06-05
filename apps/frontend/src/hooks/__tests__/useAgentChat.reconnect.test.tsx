/**
 * F-RECONNECT: queue sends across a disconnect and replay on reconnect.
 *
 * A message enqueued (or attempted) while !isConnected is parked with
 * sendState:"waiting-reconnect". On the disconnected→connected rising edge
 * the hook replays the waiting-reconnect items (re-attempts them) exactly
 * once, then resumes the normal drain. Mirrors upstream OpenClaw web UI's
 * markQueuedChatSendsWaitingForReconnect / retryReconnectableQueuedChatSends
 * (~/Desktop/openclaw/ui/src/ui/chat/chat-queue.ts:848-913).
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
let isConnected = true;

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected,
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

describe("useAgentChat — F-RECONNECT", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset();
    isConnected = true;
    vi.resetModules();
  });

  it("enqueues a send issued while disconnected as waiting-reconnect", async () => {
    isConnected = false;
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("offline send");
    });

    // Nothing went over the wire while disconnected.
    expect(sendChat).not.toHaveBeenCalled();
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("offline send");
    expect(result.current.queuedMessages[0].sendState).toBe("waiting-reconnect");
  });

  it("replays the waiting-reconnect item exactly once on reconnect", async () => {
    isConnected = false;
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("offline send");
    });
    expect(result.current.queuedMessages[0].sendState).toBe("waiting-reconnect");

    // Reconnect rising edge → replay.
    isConnected = true;
    act(() => {
      rerender();
    });

    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0]?.[1]).toBe("offline send");
    expect(result.current.queuedMessages).toHaveLength(0);

    // A subsequent reconnect cycle must not re-fire the already-replayed send.
    isConnected = false;
    act(() => rerender());
    isConnected = true;
    act(() => rerender());
    expect(sendChat).toHaveBeenCalledTimes(1);
  });
});
