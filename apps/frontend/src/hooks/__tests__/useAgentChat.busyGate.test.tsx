/**
 * Busy-gate behavior for useAgentChat.
 *
 * Covers two Codex P1 findings on PR #682:
 *   1. Tombstoned terminals: a late `done` or `error` for a runId the
 *      user already cancelled must not mutate the current live turn.
 *   2. Approval-pause: the busy gate must stay armed across the
 *      intermediate `chat.final` that OpenClaw emits while waiting on an
 *      exec-approval, and release on resolve/dismiss/auto-prune.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import type { ExecApprovalRequest } from "@/hooks/useExecApprovals";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(name: string, data: unknown) => void> = [];
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

// Approval-queue stub controlled per-test. The hook calls
// `useExecApprovalsContextOptional`, which falls back to the empty
// default when no provider is mounted — we substitute that with a
// mutable holder so tests can simulate pending/empty approval queues.
let approvalQueue: ExecApprovalRequest[] = [];
vi.mock("@/hooks/useExecApprovals", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/useExecApprovals")>(
    "@/hooks/useExecApprovals",
  );
  return {
    ...actual,
    useExecApprovalsContextOptional: () => ({
      queue: approvalQueue,
      resolve: vi.fn(),
      dismiss: vi.fn(),
      error: null,
    }),
  };
});

function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — tombstoned terminals (Codex P1 a)", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    approvalQueue = [];
    vi.resetModules();
  });

  it("late `done` for a cancelled run must not fold runB's live state into a runA-tagged message", async () => {
    // Reproduces the race Codex flagged: user cancels runA, sends a new
    // prompt that starts runB, then a stale chat.final for runA arrives.
    // Before the fix, the done handler would fold runB's live segments +
    // tools into a synthesized HistoryMessage tagged with runA's runId
    // and wipe runB's live state. After the fix it cleans up the
    // tombstone marker and returns without touching state.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();

    await act(async () => {
      await result.current.cancelMessage();
    });

    await act(async () => {
      await result.current.sendMessage("second");
    });
    const runB = lastClientRunId();
    expect(runB).not.toBe(runA);

    // runB starts streaming.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runB,
        content: "hello from runB",
      } as ChatIncomingMessage),
    );

    // Stale done for runA arrives mid-runB.
    act(() =>
      dispatch({
        type: "done",
        agent_id: "agent-X",
        runId: runA,
      } as ChatIncomingMessage),
    );

    // runB's stream is preserved — the stale done should be a no-op.
    expect(result.current.isStreaming).toBe(true);
    // No assistant HistoryMessage should have been synthesized yet.
    const assistantMessages = result.current.renderItems.filter(
      (i) => i.kind === "message" && i.message.role === "assistant",
    );
    expect(assistantMessages).toHaveLength(0);
  });

  it("late `error` for a cancelled run must not append a stale Error: bubble or wipe runB's live state", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();

    await act(async () => {
      await result.current.cancelMessage();
    });

    await act(async () => {
      await result.current.sendMessage("second");
    });
    const runB = lastClientRunId();

    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runB,
        content: "hello from runB",
      } as ChatIncomingMessage),
    );

    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        runId: runA,
        message: "Agent run was cancelled",
      } as ChatIncomingMessage),
    );

    // No "Error: …" bubble was appended; runB's stream still live.
    const assistantMessages = result.current.renderItems.filter(
      (i) => i.kind === "message" && i.message.role === "assistant",
    );
    expect(assistantMessages).toHaveLength(0);
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.error).toBeNull();
  });
});

describe("useAgentChat — approval-pause busy gate (Codex P1 b)", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    approvalQueue = [];
    vi.resetModules();
  });

  function fakeApproval(agentId: string, id = "approval-1"): ExecApprovalRequest {
    return {
      id,
      command: "ls",
      host: "gateway",
      allowedDecisions: ["allow-once", "allow-always", "deny"],
      expiresAtMs: Date.now() + 60_000,
      enqueuedAtMs: Date.now(),
      agentId,
    };
  }

  it("a follow-up sent while an approval is pending stays queued (gate stays armed across the intermediate done)", async () => {
    // Pre-seed the queue with an approval for our agent. The hook reads
    // it via useExecApprovalsContextOptional on mount and the ref mirror.
    approvalQueue = [fakeApproval("agent-X")];

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const run1 = lastClientRunId();

    // Intermediate chat.final from OpenClaw (approval-pause emit).
    act(() =>
      dispatch({
        type: "done",
        agent_id: "agent-X",
        runId: run1,
      } as ChatIncomingMessage),
    );

    // Gate must stay armed — user's follow-up should queue, not dispatch.
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(1); // only the original send
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("follow-up");
  });

  it("releases the gate (and flushes queued messages) when the approval queue empties via dismiss / resolve / auto-prune", async () => {
    approvalQueue = [fakeApproval("agent-X")];

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const run1 = lastClientRunId();

    act(() =>
      dispatch({
        type: "done",
        agent_id: "agent-X",
        runId: run1,
      } as ChatIncomingMessage),
    );

    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Simulate dismiss/resolve/auto-prune: approval queue drains.
    approvalQueue = [];
    await act(async () => {
      rerender();
    });

    // The queue-drain effect should have released the gate, and the
    // falling-edge flush effect should have dispatched the queued msg.
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("does not gate on approvals for a different agent", async () => {
    approvalQueue = [fakeApproval("agent-OTHER")];

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const run1 = lastClientRunId();

    act(() =>
      dispatch({
        type: "done",
        agent_id: "agent-X",
        runId: run1,
      } as ChatIncomingMessage),
    );

    // No approval for THIS agent, so the gate clears normally and a
    // follow-up dispatches immediately rather than queuing.
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
