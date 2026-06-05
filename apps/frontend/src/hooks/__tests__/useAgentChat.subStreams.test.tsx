/**
 * Dropped sub-stream surfacing for useAgentChat (C-track).
 *
 * The backend now forwards four formerly-dropped OpenClaw sub-streams as
 * typed chat frames: tool_progress, command_output, compaction, and
 * agent_activity. This suite drives those frames through the hook's chat
 * handler and asserts they reach the public render output (renderItems /
 * renderGroups) instead of vanishing.
 *
 * Asserts against the PUBLIC interface (renderItems) — the hook exposes
 * renderGroups/renderItems, not the internal chatMessages/chatToolMessages
 * arrays. A live tool/activity entry surfaces as a kind:"message" render item
 * whose message.content carries the tool_use block; a compaction surfaces as
 * a kind:"divider" item.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import type { ContentBlock, ChatRenderItem, HistoryMessage } from "@/hooks/internal/types";
import { useAgentChat } from "@/hooks/useAgentChat";

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
// Mirrors useAgentChat's internal throttle for running-tool syncs.
const TOOL_STREAM_THROTTLE_MS = 80;

function emit(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

function startTool() {
  emit({
    type: "tool_start",
    tool: "bash",
    toolCallId: "t1",
    agent_id: AGENT,
    runId: RUN,
  } as ChatIncomingMessage);
}

/** Find a live tool_use block by toolCallId across all render-item messages. */
function findToolUseBlock(items: ChatRenderItem[], toolCallId: string): ContentBlock | undefined {
  for (const item of items) {
    if (item.kind !== "message") continue;
    const content = (item.message as HistoryMessage).content;
    if (!Array.isArray(content)) continue;
    const block = content.find((b) => b.type === "tool_use" && b.id === toolCallId);
    if (block) return block;
  }
  return undefined;
}

/** Find a live tool_use block by its display name (used for activity pills). */
function findToolUseByName(items: ChatRenderItem[], name: string): ContentBlock | undefined {
  for (const item of items) {
    if (item.kind !== "message") continue;
    const content = (item.message as HistoryMessage).content;
    if (!Array.isArray(content)) continue;
    const block = content.find((b) => b.type === "tool_use" && b.name === name);
    if (block) return block;
  }
  return undefined;
}

describe("useAgentChat — dropped sub-stream surfacing (C)", () => {
  beforeEach(() => {
    chatHandlers = [];
    // The global afterEach (tests/setup.ts) calls restoreAllMocks, stripping
    // the resolved value — re-establish it so the history-fetch effect's
    // sendReq("chat.history") returns a thenable.
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
  });

  it("tool_progress sets progressText on the live tool entry", () => {
    // Running entries sync to chatToolMessages on an 80ms throttle (no
    // terminal force-flush), so drive the timer to flush the pill.
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useAgentChat(AGENT, "main"));
      act(() => {
        startTool();
        emit({
          type: "tool_progress",
          tool: "bash",
          toolCallId: "t1",
          partialResult: [{ type: "text", text: "line 1\n" }],
          agent_id: AGENT,
          runId: RUN,
        } as ChatIncomingMessage);
      });
      act(() => {
        vi.advanceTimersByTime(TOOL_STREAM_THROTTLE_MS);
      });
      const block = findToolUseBlock(result.current.renderItems, "t1");
      expect(block).toBeTruthy();
      expect(block!.progressText).toBe("line 1\n");
    } finally {
      vi.useRealTimers();
    }
  });

  it("command_output appends to the live tool entry progressText", () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useAgentChat(AGENT, "main"));
      act(() => {
        startTool();
        emit({
          type: "command_output",
          toolCallId: "t1",
          output: "compiling...\n",
          agent_id: AGENT,
          runId: RUN,
        } as ChatIncomingMessage);
        emit({
          type: "command_output",
          toolCallId: "t1",
          output: "done\n",
          agent_id: AGENT,
          runId: RUN,
        } as ChatIncomingMessage);
      });
      act(() => {
        vi.advanceTimersByTime(TOOL_STREAM_THROTTLE_MS);
      });
      const block = findToolUseBlock(result.current.renderItems, "t1");
      expect(block).toBeTruthy();
      expect(block!.progressText).toBe("compiling...\ndone\n");
    } finally {
      vi.useRealTimers();
    }
  });

  it("command_output for an unknown toolCallId is a no-op (no live pill)", () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({
        type: "command_output",
        toolCallId: "ghost",
        output: "x",
        agent_id: AGENT,
        runId: RUN,
      } as ChatIncomingMessage);
    });
    expect(findToolUseBlock(result.current.renderItems, "ghost")).toBeUndefined();
  });

  it("compaction end appends a compaction divider", () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({ type: "compaction", phase: "end", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    const divider = result.current.renderItems.find((i) => i.kind === "divider");
    expect(divider).toBeTruthy();
  });

  it("agent_activity (plan) creates an activity pill", () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({
        type: "agent_activity",
        activity: "plan",
        title: "Proposed a plan",
        detail: { steps: ["a"] },
        agent_id: AGENT,
        runId: RUN,
      } as ChatIncomingMessage);
    });
    expect(findToolUseByName(result.current.renderItems, "Proposed a plan")).toBeTruthy();
  });

  it("drops sub-stream events for a different agent", () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({
        type: "agent_activity",
        activity: "plan",
        title: "Other agent plan",
        detail: {},
        agent_id: "other-agent",
        runId: RUN,
      } as ChatIncomingMessage);
    });
    expect(findToolUseByName(result.current.renderItems, "Other agent plan")).toBeUndefined();
  });

  // After the user clears (or aborts) a run, that run's id is tombstoned. A
  // late sub-stream frame for it must NOT repopulate the now-empty chat with a
  // compaction divider or a stale activity pill (the latter can also re-arm the
  // busy gate via run adoption). The #811 tombstone filter only covers
  // chunk/thinking/tool_*; these C-track sub-stream branches sit after it.
  it("drops a late compaction frame for a cleared/tombstoned run", () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      // Establish the active run so clearMessages can tombstone its runId.
      emit({ type: "chunk", content: "hi", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    act(() => {
      result.current.clearMessages();
    });
    act(() => {
      // Late frame for the cleared run — must be dropped.
      emit({ type: "compaction", phase: "end", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    expect(result.current.renderItems.find((i) => i.kind === "divider")).toBeUndefined();
  });

  it("drops a late agent_activity frame for a cleared/tombstoned run", () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({ type: "chunk", content: "hi", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    act(() => {
      result.current.clearMessages();
    });
    act(() => {
      emit({
        type: "agent_activity",
        activity: "plan",
        title: "Late plan",
        detail: { steps: ["a"] },
        agent_id: AGENT,
        runId: RUN,
      } as ChatIncomingMessage);
    });
    expect(findToolUseByName(result.current.renderItems, "Late plan")).toBeUndefined();
  });
});
