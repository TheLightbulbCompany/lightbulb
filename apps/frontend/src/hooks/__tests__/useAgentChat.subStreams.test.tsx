/**
 * Dropped sub-stream surfacing for useAgentChat.
 *
 * The backend forwards formerly-dropped OpenClaw sub-streams as typed chat
 * frames: tool_progress, command_output, and agent_activity. This suite drives
 * those frames through the hook's chat handler and asserts they reach the
 * public render output (renderItems / renderGroups) instead of vanishing.
 *
 * Asserts against the PUBLIC interface (renderItems) — the hook exposes
 * renderGroups/renderItems, not the internal chatMessages/chatToolMessages
 * arrays. A live tool/activity entry surfaces as a kind:"message" render item
 * whose message.content carries the tool_use block.
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

  it("orders an activity pill AFTER streamed assistant text, not before it", () => {
    // When an agent_activity frame arrives after the assistant has streamed
    // text but before any real tool_start, the activity branch must first
    // commit the live stream tail into segments (exactly as tool_start does)
    // so foldLiveTurnIntoContent pairs the text with the pill in chronological
    // order. Without that commit, the streamed text sits in tailStream (folded
    // LAST) while the pill is folded first — rendering the pill BEFORE the
    // earlier text.
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({ type: "chunk", content: "Here is the answer.", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
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
    act(() => {
      emit({ type: "done", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });

    // The folded turn is a single assistant message; locate the content array
    // that holds both the text and the activity pill, narrowing on kind first
    // (ChatRenderItem is a discriminated union — same pattern as the helpers).
    let content: ContentBlock[] | undefined;
    for (const item of result.current.renderItems) {
      if (item.kind !== "message") continue;
      const c = (item.message as HistoryMessage).content;
      if (!Array.isArray(c)) continue;
      const hasText = c.some((b) => b.type === "text" && b.text === "Here is the answer.");
      const hasPill = c.some((b) => b.type === "tool_use" && b.name === "Proposed a plan");
      if (hasText && hasPill) {
        content = c;
        break;
      }
    }
    expect(content).toBeTruthy();
    const textIdx = content!.findIndex(
      (b) => b.type === "text" && b.text === "Here is the answer.",
    );
    const pillIdx = content!.findIndex(
      (b) => b.type === "tool_use" && b.name === "Proposed a plan",
    );
    // The streamed text must come before the activity pill.
    expect(textIdx).toBeLessThan(pillIdx);
  });

  // When the FIRST frame of an adopted run is agent_activity (a plan/patch/
  // item/acp event before any assistant chunk, thinking, or tool_start), the
  // branch must flip streaming on — exactly as chunk/thinking/tool_start do
  // when they accept a run's first frame. Otherwise the busy gate looks idle
  // (no stop affordance) and cancelMessage no-ops (it guards on isStreaming),
  // so the user can't stop an activity-first run until another stream type
  // arrives.
  it("an activity-first run marks isStreaming and makes cancelMessage effective", async () => {
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

    // The accepted activity pill marks the run busy.
    expect(result.current.isStreaming).toBe(true);

    // cancelMessage is now effective: it fires a targeted agent_abort for the
    // adopted run instead of no-opping on a false-idle gate.
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: AGENT,
      runId: RUN,
    });
  });

  // When a tab reconnects/opens mid-run, the first observed frame after the
  // original tool_start can be a tool_progress for a toolCallId this tab has
  // no live pill for. The generic run-adoption block sets clientRunIdRef, but
  // the tool_progress branch returned early on the missing pill WITHOUT
  // flipping streaming — so the adopted run looked idle (no stop affordance)
  // and cancelMessage no-opped (it guards on isStreaming). The branch must
  // mark streaming for an observed in-flight run even with no local pill,
  // exactly as tool_start does.
  it("an observed tool_progress-first run marks isStreaming and makes cancelMessage effective", async () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      // First frame this tab sees for the run is a progress frame for a
      // toolCallId it never saw start (no live pill).
      emit({
        type: "tool_progress",
        tool: "bash",
        toolCallId: "observed-1",
        partialResult: [{ type: "text", text: "line 1\n" }],
        agent_id: AGENT,
        runId: RUN,
      } as ChatIncomingMessage);
    });

    // The observed in-flight run marks the gate busy even without a local pill.
    expect(result.current.isStreaming).toBe(true);

    // cancelMessage is now effective: it fires a targeted agent_abort for the
    // adopted run instead of no-opping on a false-idle gate.
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: AGENT,
      runId: RUN,
    });
  });

  // Same false-idle hazard via command_output: a reconnecting tab's first
  // observed frame is streamed stdout for a tool it has no pill for. The
  // branch must flip streaming for the adopted run, mirroring tool_start.
  it("an observed command_output-first run marks isStreaming and makes cancelMessage effective", async () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      emit({
        type: "command_output",
        toolCallId: "observed-2",
        output: "compiling...\n",
        agent_id: AGENT,
        runId: RUN,
      } as ChatIncomingMessage);
    });

    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(send).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: AGENT,
      runId: RUN,
    });
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
  // stale activity pill (which can also re-arm the busy gate via run adoption).
  // The tombstone filter only covers chunk/thinking/tool_*; these sub-stream
  // branches sit after it.
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

  // clearMessages tombstones the active runId AND nulls clientRunIdRef. A late
  // sub-stream for that run must be dropped BEFORE the generic run-adoption
  // block — otherwise adoption (which fires whenever clientRunIdRef is null)
  // re-adopts the tombstoned runId, re-arming the busy gate so the user's NEXT
  // send is queued behind a dead run instead of dispatching. The drop must
  // therefore precede adoption, not merely happen later in the sub-stream
  // branches.
  it("a late sub-stream for a cleared run does not re-adopt it (next send still dispatches)", async () => {
    const { result } = renderHook(() => useAgentChat(AGENT, "main"));
    act(() => {
      // Establish the active run, then clear it (tombstones RUN, nulls
      // clientRunIdRef).
      emit({ type: "chunk", content: "hi", agent_id: AGENT, runId: RUN } as ChatIncomingMessage);
    });
    act(() => {
      result.current.clearMessages();
    });
    // Late sub-stream for the cleared run. If it re-adopts RUN, the busy gate
    // re-arms and the next send queues.
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

    await act(async () => {
      await result.current.sendMessage("after clear");
    });

    // The gate must be clear: the send dispatched (sendChat fired) and nothing
    // was parked behind the re-adopted dead run.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0]?.[1]).toBe("after clear");
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
