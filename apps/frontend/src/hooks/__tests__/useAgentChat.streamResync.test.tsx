/**
 * Non-append chunk frames carry SEGMENT-scoped text (upstream resets its
 * accumulator at every assistant message_start, i.e. after every tool call) —
 * NOT run-cumulative text. See packages/shared/src/gateway/streamText.ts.
 *
 * The chunk handler must rebuild them as frozen-prefix + segment text.
 * Folding them through accumulateStreamText replaced the run-cumulative
 * buffer with the (shorter) segment text, so the slice at the committed
 * offset came back empty and every post-tool resync — including the
 * chat.final backstop that ends EVERY run — wiped the final segment from
 * the synthesized message until a reload refetched history.
 *
 * Mirrors the useAgentChat.slash.test.tsx harness (renderHook + mocked
 * useGateway); the mobile agent screen's chunk handler is the reference
 * implementation for the frozen-prefix rebuild.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn<(method: string, params?: unknown) => Promise<unknown>>();
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

/** Serialized content of the last persisted assistant message (folded content
 * is an array of blocks — serialize so toContain checks the text inside). */
function lastAssistantContent(result: {
  current: { chatMessages: Array<{ role?: string; content?: unknown }> };
}): string {
  const assistants = result.current.chatMessages.filter((m) => m.role === "assistant");
  return assistants.length > 0 ? JSON.stringify(assistants[assistants.length - 1].content) : "";
}

describe("useAgentChat — segment-scoped non-append resync frames", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("keeps the post-tool segment when it arrives as a non-append resync (chat.final backstop)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("create the goal");
    });
    const runA = lastClientRunId();

    // Segment 1 streams, then a tool call commits it (offset advances past it).
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: "I'll create that as a company goal in Paperclip.",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "tool_start",
        agentId: "agent-X",
        runId: runA,
        tool: "exec",
        toolCallId: "t1",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "tool_end",
        agentId: "agent-X",
        runId: runA,
        tool: "exec",
        toolCallId: "t1",
      } as ChatIncomingMessage),
    );

    // Post-tool segment arrives as a NON-append frame (continuity was cleared,
    // or this is the chat.final backstop). Its content is the SEGMENT text —
    // shorter than the committed offset.
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: "Created the company goal.",
      } as ChatIncomingMessage),
    );
    act(() => dispatch({ type: "done", agentId: "agent-X", runId: runA } as ChatIncomingMessage));

    const content = lastAssistantContent(result);
    expect(content).toContain("I'll create that as a company goal in Paperclip.");
    expect(content).toContain("Created the company goal.");
  });

  it("append deltas after a post-tool resync extend the current segment", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("create the goal");
    });
    const runA = lastClientRunId();

    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: "A long first segment that the tool call commits.",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "tool_start",
        agentId: "agent-X",
        runId: runA,
        tool: "exec",
        toolCallId: "t1",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "tool_end",
        agentId: "agent-X",
        runId: runA,
        tool: "exec",
        toolCallId: "t1",
      } as ChatIncomingMessage),
    );
    // Resync opens the new segment, then deltas keep extending it.
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: "Created",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: " the goal.",
        append: true,
      } as ChatIncomingMessage),
    );
    act(() => dispatch({ type: "done", agentId: "agent-X", runId: runA } as ChatIncomingMessage));

    const content = lastAssistantContent(result);
    expect(content).toContain("A long first segment that the tool call commits.");
    expect(content).toContain("Created the goal.");
  });

  it("does not duplicate text committed by an activity pill when the final non-append frame carries the whole message", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("plan something");
    });
    const runA = lastClientRunId();

    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: "Before the plan.",
      } as ChatIncomingMessage),
    );
    // Activity pill splits the stream WITHOUT an assistant-message reset —
    // the same message keeps accumulating upstream.
    act(() =>
      dispatch({
        type: "agent_activity",
        agentId: "agent-X",
        runId: runA,
        activity: "plan",
        title: "Proposed plan",
        detail: { steps: 1 },
      } as unknown as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: " After the plan.",
        append: true,
      } as ChatIncomingMessage),
    );
    // chat.final backstop: the WHOLE current message, including the part the
    // activity pill already committed.
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId: runA,
        content: "Before the plan. After the plan.",
      } as ChatIncomingMessage),
    );
    act(() => dispatch({ type: "done", agentId: "agent-X", runId: runA } as ChatIncomingMessage));

    const content = lastAssistantContent(result);
    expect(content).toContain("Before the plan.");
    expect(content).toContain("After the plan.");
    // The committed prefix must fold exactly once.
    expect(content.match(/Before the plan\./g)).toHaveLength(1);
  });
});
