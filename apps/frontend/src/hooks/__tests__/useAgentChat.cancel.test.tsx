/**
 * F-CANCEL: targeted, server-reconciled abort for useAgentChat.
 *
 * cancelMessage() sends a targeted `agent_abort` carrying the active
 * clientRunId and does NOT locally clear isStreaming / live state — it
 * waits for the server's terminal (`run_ended` outcome "aborted") to drive
 * the teardown. Mirrors upstream OpenClaw web UI's handleAbortChat
 * (upstream ui/src/ui/app-chat.ts): server-reconcile drives
 * done, not a local edge.
 *
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";
import { resolveMessageBlocks } from "@/hooks/internal/toolBlockExtract";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

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

describe("useAgentChat — F-CANCEL targeted server-reconciled abort", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
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
      dispatch({ type: "chunk", agentId: "agent-X", runId: runA, content: "live" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.cancelMessage();
    });

    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agentId: "agent-X",
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
      dispatch({ type: "chunk", agentId: "agent-X", runId: runA, content: "live" } as ChatIncomingMessage),
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
        agentId: "agent-X",
        runId: runA,
        message: "Agent run was cancelled",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "run_ended",
        agentId: "agent-X",
        runId: runA,
        outcome: "aborted",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);
  });

  it("renders the abort as a calm notice, not an error", async () => {
    // The backend delivers a user-initiated Stop as an `error` frame
    // (connection_pool.py's chat.aborted transform). It is the outcome the
    // user asked for, so it must NOT get the "Error: " prefix or an errorCode
    // — an errorCode is what MessageList upgrades into a red ChatErrorCard.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agentId: "agent-X", runId: runA, content: "live" } as ChatIncomingMessage),
    );

    await act(async () => {
      await result.current.cancelMessage();
    });
    act(() =>
      dispatch({
        type: "error",
        agentId: "agent-X",
        runId: runA,
        message: "Agent run was cancelled",
      } as ChatIncomingMessage),
    );

    const items = result.current.renderGroups.flatMap((g) => g.items);
    const notice = items.find(
      (i) =>
        i.kind === "message" &&
        (resolveMessageBlocks(i.message)[0]?.text ?? "").includes(
          "Agent run was cancelled",
        ),
    );
    expect(notice, "expected the abort notice in the transcript").toBeDefined();
    const msg = (notice as { message: { text?: string; content?: { text?: string }[]; errorCode?: string } }).message;
    const text = msg.content?.[0]?.text ?? msg.text ?? "";
    expect(text).toBe("Agent run was cancelled");
    expect(text.startsWith("Error: ")).toBe(false);
    expect(msg.errorCode).toBeUndefined();
  });
});
