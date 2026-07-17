/**
 * Stream paint throttle + append-delta reconstruction for useAgentChat.
 *
 * Chunk/thinking frames update refs synchronously but paint on an 80ms
 * throttle (mirroring the tool-stream throttle): the FIRST chunk of a run
 * paints immediately (run boundary), later chunks coalesce. Frames arrive
 * either as full-text (legacy / resync) or append deltas (`append: true`);
 * accumulateStreamText reconstructs the cumulative text, and `done` folds
 * from the refs, so a pending unpainted delta is never lost.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import { useAgentChat } from "@/hooks/useAgentChat";
import type { ChatRenderItem, HistoryMessage } from "@/hooks/internal/types";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
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

vi.mock("@/hooks/useExecApprovals", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/useExecApprovals")>(
    "@/hooks/useExecApprovals",
  );
  return {
    ...actual,
    useExecApprovalsContextOptional: () => ({
      queue: [],
      resolve: vi.fn(),
      dismiss: vi.fn(),
      onApprovalRemoved: () => () => {},
      error: null,
    }),
  };
});

const AGENT = "agent-x";
const RUN = "run-1";
const STREAM_PAINT_THROTTLE_MS = 80;

function emit(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

function streamText(items: ChatRenderItem[]): string | null {
  const item = items.find((i) => i.kind === "stream");
  return item && item.kind === "stream" ? item.text : null;
}

function thinkingText(items: ChatRenderItem[]): string | null {
  const item = items.find((i) => i.kind === "live-thinking");
  return item && item.kind === "live-thinking" ? item.text : null;
}

describe("useAgentChat — stream paint throttle + append deltas", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("paints the first chunk of a run immediately, throttles later chunks", () => {
    const { result } = renderHook(() => useAgentChat(AGENT));

    act(() => {
      emit({ type: "chunk", content: "He", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    // Run boundary → immediate paint, no 80ms of invisible first text.
    expect(streamText(result.current.renderItems)).toBe("He");

    act(() => {
      emit({ type: "chunk", content: "Hello", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    // Same run → paint deferred to the throttle window.
    expect(streamText(result.current.renderItems)).toBe("He");

    act(() => {
      vi.advanceTimersByTime(STREAM_PAINT_THROTTLE_MS);
    });
    expect(streamText(result.current.renderItems)).toBe("Hello");
  });

  it("reconstructs append deltas into cumulative text", () => {
    const { result } = renderHook(() => useAgentChat(AGENT));

    act(() => {
      emit({ type: "chunk", content: "He", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      emit({ type: "chunk", content: "llo", append: true, agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      emit({ type: "chunk", content: " world", append: true, agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      vi.advanceTimersByTime(STREAM_PAINT_THROTTLE_MS);
    });
    expect(streamText(result.current.renderItems)).toBe("Hello world");
  });

  it("a full-text (non-append) frame replaces the buffer — resync semantics", () => {
    const { result } = renderHook(() => useAgentChat(AGENT));

    act(() => {
      emit({ type: "chunk", content: "garbled", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      emit({ type: "chunk", content: "Corrected full text", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      vi.advanceTimersByTime(STREAM_PAINT_THROTTLE_MS);
    });
    expect(streamText(result.current.renderItems)).toBe("Corrected full text");
  });

  it("done folds from refs — a pending unpainted delta is never lost", () => {
    const { result } = renderHook(() => useAgentChat(AGENT));

    act(() => {
      emit({ type: "chunk", content: "Answer", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      emit({ type: "chunk", content: " part two", append: true, agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      // done arrives BEFORE the throttle window elapses.
      emit({ type: "done", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });

    const folded = result.current.renderItems.find(
      (i): i is ChatRenderItem & { kind: "message"; message: HistoryMessage } =>
        i.kind === "message" && (i.message as HistoryMessage).role === "assistant",
    );
    expect(folded).toBeTruthy();
    const text = (folded!.message.content as Array<{ type: string; text?: string }>)
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    expect(text).toBe("Answer part two");
    // Live stream item is gone after the fold.
    expect(streamText(result.current.renderItems)).toBeNull();
  });

  it("accumulates thinking deltas into the live-thinking item", () => {
    const { result } = renderHook(() => useAgentChat(AGENT));

    act(() => {
      emit({ type: "thinking", content: "Let me ", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      emit({ type: "thinking", content: "think", append: true, agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
      vi.advanceTimersByTime(STREAM_PAINT_THROTTLE_MS);
    });
    expect(thinkingText(result.current.renderItems)).toBe("Let me think");
  });
});
