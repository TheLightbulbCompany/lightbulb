/**
 * F-STEER: inject a queued message into the live run.
 *
 * steerMessage(id) only acts when a run is active (clientRunIdRef set). It
 * sends the queued text through the canonical HTTP steer endpoint, then
 * removes the item after the request succeeds. No-op if no run is active.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";
import { notifyApiError } from "@/lib/notify";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({});
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useAgentRoomTransport", () => ({
  useAgentRoomTransport: (agentId: string | null) => ({
    isConnected: true,
    onChatMessage: (handler: (message: ChatIncomingMessage) => void) => {
      chatHandlers.push(handler);
      return () => {
        chatHandlers = chatHandlers.filter((candidate) => candidate !== handler);
      };
    },
    sendRoomMessage: async (message: string, clientMessageId: string) =>
      sendChat(agentId, message, clientMessageId, undefined),
    abortRun: async (runId: string) => {
      const accepted = send({ type: "agent_abort", agentId: agentId, runId });
      if (accepted === false) throw new Error("Stop not sent");
      return {};
    },
    steerRun: async (runId: string, text: string) => {
      const accepted = send({ type: "agent_steer", agentId: agentId, runId, text });
      if (accepted === false) throw new Error("Steer not sent");
      return {};
    },
    rpc: sendReq,
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
      dispatch({ type: "chunk", agentId: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("steer me");
    });
    expect(result.current.queuedMessages).toHaveLength(1);
    const id = result.current.queuedMessages[0].id;

    await act(async () => {
      result.current.steerMessage(id);
      await Promise.resolve();
    });

    expect(send).toHaveBeenCalledWith({
      type: "agent_steer",
      agentId: "agent-X",
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
      dispatch({ type: "chunk", agentId: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("queued");
    });
    const id = result.current.queuedMessages[0].id;

    // The run ends — clientRunIdRef clears (and the item drains as runB).
    act(() =>
      dispatch({ type: "run_ended", agentId: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
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
      dispatch({ type: "chunk", agentId: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
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
      dispatch({ type: "chunk", agentId: "agent-X", runId: r1, content: "working" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    act(() =>
      dispatch({
        type: "error",
        agentId: "agent-X",
        code: "steer_failed",
        message: "Failed to steer: boom",
      } as ChatIncomingMessage),
    );

    // The live primary run is untouched: still streaming, and a new send still
    // QUEUES (gate still armed) — proving clientRunIdRef wasn't cleared.
    expect(result.current.isStreaming).toBe(true);
    expect(notifyApiError).toHaveBeenCalled();
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
      dispatch({ type: "done", agentId: "agent-X", runId: r1 } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
