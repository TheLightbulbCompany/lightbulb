/**
 * F-CANCEL: targeted, server-reconciled abort for useAgentChat.
 *
 * cancelMessage() sends a targeted `agent_abort` carrying the active
 * clientRunId and does NOT locally clear isStreaming / live state — it
 * waits for the server's terminal (`run_ended` outcome "aborted") to drive
 * the teardown. Mirrors upstream OpenClaw web UI's handleAbortChat
 * (~/Desktop/openclaw/ui/src/ui/app-chat.ts:201): server-reconcile drives
 * done, not a local edge.
 *
 * When disconnected, the abort is stored as a pending intent and replayed
 * on the next reconnect (isConnected rising edge) — mirror app-chat.ts:211.
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

function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — F-CANCEL targeted server-reconciled abort", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    isConnected = true;
    vi.resetModules();
  });

  it("sends a targeted agent_abort carrying the active runId", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "live" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.cancelMessage();
    });

    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: "agent-X",
      runId: runA,
    });
  });

  it("does NOT locally drop isStreaming — waits for the server terminal", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "live" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.cancelMessage();
    });

    // Still streaming — the server hasn't reconciled yet.
    expect(result.current.isStreaming).toBe(true);

    // Server reconciles: the aborted run's terminal arrives as error + run_ended.
    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        runId: runA,
        message: "Agent run was cancelled",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: runA,
        outcome: "aborted",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);
  });

  it("when disconnected, stores the pending abort and replays it on reconnect", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "live" } as ChatIncomingMessage),
    );

    // Connection drops.
    isConnected = false;
    rerender();

    await act(async () => {
      await result.current.cancelMessage();
    });
    // No abort sent while disconnected.
    expect(send).not.toHaveBeenCalled();

    // Reconnect (rising edge) → the stored abort replays exactly once.
    isConnected = true;
    rerender();

    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: "agent-X",
      runId: runA,
    });
    expect(send).toHaveBeenCalledTimes(1);

    // A second reconnect cycle must not re-fire it.
    isConnected = false;
    rerender();
    isConnected = true;
    rerender();
    expect(send).toHaveBeenCalledTimes(1);
  });
});
