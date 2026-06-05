/**
 * F-STEER: inject a queued message into the live run.
 *
 * steerMessage(id) only acts when a run is active (clientRunIdRef set). It
 * sends {type:"agent_steer", agent_id, text, runId} for that queued item via
 * the gateway send primitive, then removes the item from the normal queue
 * (it's now injected into the live run). No-op if no active run. Mirrors
 * upstream OpenClaw web UI's steerQueuedChatMessage
 * (~/Desktop/openclaw/ui/src/ui/app-chat.ts:737).
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

describe("useAgentChat — F-STEER", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("sends agent_steer for the queued item and removes it from the queue", async () => {
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
      await result.current.sendMessage("steer me");
    });
    expect(result.current.queuedMessages).toHaveLength(1);
    const id = result.current.queuedMessages[0].id;

    act(() => {
      result.current.steerMessage(id);
    });

    expect(send).toHaveBeenCalledWith({
      type: "agent_steer",
      agent_id: "agent-X",
      text: "steer me",
      runId: runA,
    });
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("is a no-op when no run is active", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // No active run, but a queued item somehow exists — steering it must
    // not send anything (it should drain via the normal path instead).
    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("queued");
    });
    const id = result.current.queuedMessages[0].id;

    // The run ends — clientRunIdRef clears (and the item drains as runB).
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    // The queued item has already drained — steering a now-absent id is a no-op.
    act(() => {
      result.current.steerMessage(id);
    });
    expect(send).not.toHaveBeenCalled();
  });

  it("is a no-op for an unknown queued id", async () => {
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
      await result.current.sendMessage("queued");
    });

    act(() => {
      result.current.steerMessage("nonexistent-id");
    });
    expect(send).not.toHaveBeenCalled();
    expect(result.current.queuedMessages).toHaveLength(1);
  });

  it("a steer_failed error is NON-terminal — it must not tear down the live primary run", async () => {
    // A steer is a mid-run injection into an already-active run; its failure
    // does not end that run. The backend tags it `code:"steer_failed"` so the
    // frontend surfaces a notice without clearing clientRunIdRef + isStreaming
    // (a generic no-runId error would wipe the still-streaming response).
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("primary");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: r1, content: "working" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        code: "steer_failed",
        message: "Failed to steer: boom",
      } as ChatIncomingMessage),
    );

    // The live primary run is untouched: still streaming, and a new send still
    // QUEUES (gate still armed) — proving clientRunIdRef wasn't cleared.
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.error).toBeTruthy();
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["follow-up"]);

    // No "Error:" bubble was folded into the live turn.
    const errorBubbles = result.current.renderItems.filter(
      (i) =>
        i.kind === "message" &&
        i.message.role === "assistant" &&
        Array.isArray(i.message.content) &&
        i.message.content.some((b) => b.type === "text" && (b.text ?? "").startsWith("Error:")),
    );
    expect(errorBubbles).toHaveLength(0);

    // The primary run ends normally → its terminal releases the gate and the
    // queued follow-up drains (proving the run was still the active one after
    // the steer_failed notice — clientRunIdRef was never cleared by it).
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
