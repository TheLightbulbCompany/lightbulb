/**
 * Editable queues: edit-in-place and reorder for queued prompts, without
 * disturbing the active run. Ports upstream OpenClaw 2026.8.1 "editable
 * queues" behaviour (#121682/#121692/#124298) against our simpler
 * QueuedMessage shape — array order IS send order (see drainQueue), and
 * nothing "in flight" ever lives in `queuedMessages` (the active run is
 * tracked separately via clientRunIdRef), so there is no locked-segment
 * concept to protect a move from crossing.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({});
const sendChat = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send: vi.fn(),
    sendReq,
    sendChat,
    onEvent: () => () => {},
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

describe("useAgentChat — editable queues", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    vi.resetModules();
  });

  it("edit updates text and keeps position in the queue", async () => {
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
      await result.current.sendMessage("q1");
    });
    await act(async () => {
      await result.current.sendMessage("q2");
    });
    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["q1", "q2"]);
    const q1Id = result.current.queuedMessages[0]!.id;

    act(() => {
      result.current.editQueuedMessage(q1Id, "q1 edited");
    });

    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["q1 edited", "q2"]);
    expect(result.current.queuedMessages[0]!.id).toBe(q1Id);
  });

  it("does not blank a queued message when edited to empty/whitespace text", async () => {
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
      await result.current.sendMessage("q1");
    });
    const q1Id = result.current.queuedMessages[0]!.id;

    act(() => {
      result.current.editQueuedMessage(q1Id, "   ");
    });

    expect(result.current.queuedMessages[0]!.text).toBe("q1");
  });

  it("reorder changes send order", async () => {
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
      await result.current.sendMessage("q1");
    });
    await act(async () => {
      await result.current.sendMessage("q2");
    });
    const q2Id = result.current.queuedMessages[1]!.id;

    act(() => {
      result.current.moveQueuedMessage(q2Id, "up");
    });
    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["q2", "q1"]);

    // Drain: the run ending must now send q2 first, matching the new order —
    // display order and send order are the same array.
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    expect(sendChat.mock.calls[1]?.[1]).toBe("q2");
    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["q1"]);
  });

  it("clamps a move at the queue's edges", async () => {
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
      await result.current.sendMessage("q1");
    });
    await act(async () => {
      await result.current.sendMessage("q2");
    });
    const q1Id = result.current.queuedMessages[0]!.id;
    const q2Id = result.current.queuedMessages[1]!.id;

    act(() => {
      result.current.moveQueuedMessage(q1Id, "up"); // already first
    });
    expect(result.current.queuedMessages.map((m) => m.id)).toEqual([q1Id, q2Id]);

    act(() => {
      result.current.moveQueuedMessage(q2Id, "down"); // already last
    });
    expect(result.current.queuedMessages.map((m) => m.id)).toEqual([q1Id, q2Id]);
  });

  it("editing/reordering never touches the in-flight message", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.sendMessage("q1");
    });
    const q1Id = result.current.queuedMessages[0]!.id;

    // An id that isn't in the queue (e.g. the active run's own id) resolves
    // to nothing — no crash, no send, no change.
    act(() => {
      result.current.editQueuedMessage("not-a-queued-id", "should no-op");
      result.current.moveQueuedMessage("not-a-queued-id", "up");
    });
    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["q1"]);
    expect(result.current.isStreaming).toBe(true);
    expect(sendChat).toHaveBeenCalledTimes(1); // only the original send

    // Editing the queued row itself still leaves the live run alone.
    act(() => {
      result.current.editQueuedMessage(q1Id, "q1 edited");
    });
    expect(result.current.isStreaming).toBe(true);
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0]?.[2]).toBe(runA);
  });
});
