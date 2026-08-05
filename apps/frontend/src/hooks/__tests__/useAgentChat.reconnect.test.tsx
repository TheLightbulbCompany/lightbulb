/**
 * F-RECONNECT: queue sends across a disconnect and replay on reconnect.
 *
 * A message enqueued (or attempted) while !isConnected is parked with
 * sendState:"waiting-reconnect". On the disconnected→connected rising edge
 * the hook replays the waiting-reconnect items (re-attempts them) exactly
 * once, then resumes the normal drain. Mirrors upstream OpenClaw web UI's
 * markQueuedChatSendsWaitingForReconnect / retryReconnectableQueuedChatSends
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
    send.mockReset().mockReturnValue(true);
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

  it("parks a cancel's abort when the raw send no-ops under a stale isConnected, and replays it on reconnect", async () => {
    // The stale-isConnected race: the socket closed but React hasn't
    // re-rendered isConnected=false yet, so cancelMessage takes the connected
    // path and the raw `send` silently drops (returns false). Because cancel is
    // server-reconciled (no local teardown), a dropped abort would hang the
    // chat — so the intent must be parked and replayed on reconnect.
    isConnected = true;
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("primary");
    });
    const runId = sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
    act(() =>
      chatHandlers.forEach((h) =>
        h({ type: "chunk", agent_id: "agent-X", runId, content: "x" } as ChatIncomingMessage),
      ),
    );
    expect(result.current.isStreaming).toBe(true);

    // Socket secretly closed: the abort send no-ops (returns false) this once.
    send.mockReturnValueOnce(false);
    await act(async () => {
      await result.current.cancelMessage();
    });

    const abortCalls = () =>
      send.mock.calls.filter((c) => (c[0] as { type?: string })?.type === "agent_abort");
    // It was attempted on the wire but didn't land; the run stays streaming
    // (server-reconciled cancel does not tear down locally).
    expect(abortCalls()).toHaveLength(1);
    expect(result.current.isStreaming).toBe(true);

    // Reconnect rising edge → the parked abort replays (this time it lands).
    isConnected = false;
    act(() => rerender());
    isConnected = true;
    act(() => rerender());
    expect(abortCalls()).toHaveLength(2);
    expect(abortCalls()[1]?.[0]).toMatchObject({ type: "agent_abort", runId });
  });

  it("re-parks a drained item instead of dropping it when dispatch is refused pre-wire", async () => {
    // Park a send while disconnected, then reconnect with NO agent selected.
    // The rising-edge replay dequeues the item and calls dispatchNow, whose
    // pre-wire guard refuses (no agent). The item must come back as a
    // visible failed/parked entry — never vanish.
    isConnected = false;
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(
      ({ agentId }: { agentId: string | null }) => useAgentChat(agentId, "session-1"),
      { initialProps: { agentId: "agent-X" as string | null } },
    );

    await act(async () => {
      await result.current.sendMessage("must not vanish");
    });
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].sendState).toBe("waiting-reconnect");

    // Agent deselected, then the socket comes back.
    act(() => rerender({ agentId: null }));
    isConnected = true;
    await act(async () => {
      rerender({ agentId: null });
    });

    // Nothing went over the wire, and the message is still visible in the
    // queue (failed → retryable), not silently dropped.
    expect(sendChat).not.toHaveBeenCalled();
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("must not vanish");
    expect(result.current.queuedMessages[0].sendState).toBe("failed");
  });
});
