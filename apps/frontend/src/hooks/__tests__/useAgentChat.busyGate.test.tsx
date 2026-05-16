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
// mutable holder so tests can simulate pending/empty approval queues
// AND fire reason-tagged removal notifications.
let approvalQueue: ExecApprovalRequest[] = [];
const removalHandlers: Set<
  (id: string, reason: "resolved" | "dismissed" | "auto_pruned" | "terminal_error") => void
> = new Set();
function notifyRemoval(
  id: string,
  reason: "resolved" | "dismissed" | "auto_pruned" | "terminal_error",
) {
  approvalQueue = approvalQueue.filter((a) => a.id !== id);
  removalHandlers.forEach((h) => h(id, reason));
}
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
      onApprovalRemoved: (
        h: (id: string, reason: "resolved" | "dismissed" | "auto_pruned" | "terminal_error") => void,
      ) => {
        removalHandlers.add(h);
        return () => {
          removalHandlers.delete(h);
        };
      },
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

describe("useAgentChat — approval-pause busy gate (Codex P1 b/c)", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    approvalQueue = [];
    removalHandlers.clear();
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

    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("follow-up");
  });

  it("releases the gate when the approval is dismissed (queued messages flush)", async () => {
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
      dispatch({ type: "done", agent_id: "agent-X", runId: run1 } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // User dismisses the stuck card. In real usage the queue state lives
    // in useExecApprovals — setQueue propagates through context and
    // re-renders consumers. The mock just mutates a variable, so we
    // rerender explicitly to mirror that propagation; the falling-edge
    // flush effect runs on every render and picks up the gate release.
    await act(async () => {
      notifyRemoval("approval-1", "dismissed");
      rerender();
    });

    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("releases the gate on auto_pruned and terminal_error too", async () => {
    approvalQueue = [fakeApproval("agent-X", "ap-prune")];
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );

    await act(async () => {
      notifyRemoval("ap-prune", "auto_pruned");
    });
    await act(async () => {
      await result.current.sendMessage("after-prune");
    });
    expect(sendChat).toHaveBeenCalledTimes(2);

    // And terminal_error path.
    approvalQueue = [fakeApproval("agent-X", "ap-bad")];
    const r2 = lastClientRunId();
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r2 } as ChatIncomingMessage),
    );
    await act(async () => {
      notifyRemoval("ap-bad", "terminal_error");
    });
    await act(async () => {
      await result.current.sendMessage("after-error");
    });
    expect(sendChat).toHaveBeenCalledTimes(3);
  });

  it("keeps the gate armed on `resolved` AND transfers it to the fresh continuation runId (Codex P1 c)", async () => {
    // After resolve, OpenClaw's continuation runs as a FRESH server-side
    // run with its own runId — the original run truly ended at the
    // intermediate chat.final. The gate must (a) stay armed across the
    // queue-drain → first-chunk window AND (b) transfer to the fresh
    // runId so the continuation's terminal `done` releases it. Without
    // the transfer the gate would stay armed forever (runId mismatch).
    approvalQueue = [fakeApproval("agent-X", "ap-res")];
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );

    // Server-side resolve: queue drains with reason="resolved". The
    // subscriber arms resumeExpectedRef without releasing the gate.
    await act(async () => {
      notifyRemoval("ap-res", "resolved");
    });

    // A follow-up typed in the continuation window stays queued.
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);

    // Continuation arrives on a FRESH runId — the gate must transfer.
    const r2 = "server-resume-run-id-fresh";
    expect(r2).not.toBe(r1);
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: r2,
        content: "continuation",
      } as ChatIncomingMessage),
    );
    // Terminal done arrives on the fresh runId — gate releases, queued
    // follow-up flushes.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r2 } as ChatIncomingMessage),
    );

    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("keeps the gate armed when one approval is dismissed but siblings for this agent remain (Codex P2)", async () => {
    // Multiple approvals can be pending on the same run (e.g. parallel
    // tools). Dismissing/expiring one must NOT release the gate while
    // siblings are still queued — the agent is still effectively
    // paused on them, and a follow-up would race.
    approvalQueue = [
      fakeApproval("agent-X", "ap-a"),
      fakeApproval("agent-X", "ap-b"),
    ];
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );

    // Dismiss one of two siblings.
    await act(async () => {
      notifyRemoval("ap-a", "dismissed");
      rerender();
    });

    // Gate must still be armed → follow-up queues.
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);

    // Now dismiss the remaining sibling — gate releases, queued flush.
    await act(async () => {
      notifyRemoval("ap-b", "dismissed");
      rerender();
    });
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
  });

  it("clears isStreaming when releasing the gate on dismiss (Codex P2)", async () => {
    // The interim `done` skipped setIsStreaming(false) because the gate
    // was held for the approval. When the user dismisses the stuck
    // card, the gate-release path must also clear isStreaming —
    // otherwise the chat shows the stop button indefinitely with no
    // live run.
    approvalQueue = [fakeApproval("agent-X", "ap-stuck")];
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );
    // Sanity: the approval-pause held the gate so isStreaming stayed on.
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      notifyRemoval("ap-stuck", "dismissed");
      rerender();
    });

    expect(result.current.isStreaming).toBe(false);
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
      dispatch({ type: "done", agent_id: "agent-X", runId: run1 } as ChatIncomingMessage),
    );

    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
