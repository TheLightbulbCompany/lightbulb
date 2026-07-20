/**
 * Busy-gate behavior for useAgentChat.
 *
 * Two invariants:
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

describe("useAgentChat — tombstoned terminals", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    approvalQueue = [];
    vi.resetModules();
  });

  it("late `done` for a cleared run must not fold runB's live state into a runA-tagged message", async () => {
    // The race: user clears runA (tombstoning it), sends a new prompt that
    // starts runB, then a stale chat.final for runA arrives. Without the
    // tombstone path, the done handler folds runB's live segments + tools
    // into a synthesized HistoryMessage tagged with runA's runId and wipes
    // runB's live state. The tombstone marker swallows the late terminal and
    // returns without touching state.
    //
    // (Cancel is now server-reconciled and does NOT tombstone locally —
    // clearMessages is the remaining local tombstone trigger.)
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();

    act(() => {
      result.current.clearMessages();
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

  it("late `error` for a cleared run must not append a stale Error: bubble or wipe runB's live state", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();

    act(() => {
      result.current.clearMessages();
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
  });
});

describe("useAgentChat — approval-pause busy gate", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    approvalQueue = [];
    removalHandlers.clear();
    vi.resetModules();
  });

  function fakeApproval(agentId: string, id = "approval-1"): ExecApprovalRequest {
    return {
      id,
      kind: "exec",
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

  it("keeps the gate armed on `resolved` AND transfers it to the fresh continuation runId", async () => {
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

  it("preserves the approved tool's result by appending tool_result to the synthesized history message", async () => {
    // Pre-approval flow folds the tool_use into a HistoryMessage at
    // the interim chat.final and clears toolStreamByIdRef. When the
    // continuation's tool_end arrives, the live-state lookup returns
    // undefined — without the history fallback, the approved command's
    // output would be silently dropped.
    approvalQueue = [fakeApproval("agent-X", "ap-output")];
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("run a thing");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({
        type: "tool_start",
        agent_id: "agent-X",
        runId: r1,
        tool: "exec",
        toolCallId: "tc-output",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );
    await act(async () => {
      notifyRemoval("ap-output", "resolved");
      rerender();
    });

    const r2 = "fresh-runid-with-result";
    act(() =>
      dispatch({
        type: "tool_end",
        agent_id: "agent-X",
        runId: r2,
        tool: "exec",
        toolCallId: "tc-output",
        result: [{ type: "text", text: "command output here" }],
      } as ChatIncomingMessage),
    );

    // The synthesized assistant message should now carry a
    // tool_result block matching the tool_use id.
    const assistantMsg = result.current.renderItems
      .filter((i) => i.kind === "message" && i.message.role === "assistant")
      .map((i) => (i.kind === "message" ? i.message : null))
      .find(
        (m) =>
          m && Array.isArray(m.content) &&
          m.content.some((b) => b.type === "tool_use" && b.id === "tc-output"),
      );
    expect(assistantMsg).toBeTruthy();
    const result_block = assistantMsg!.content!.find(
      (b) => b.type === "tool_result" && b.tool_use_id === "tc-output",
    );
    expect(result_block).toBeTruthy();
    expect(result_block).toMatchObject({
      type: "tool_result",
      tool_use_id: "tc-output",
    });
  });

  it("server-reconciled cancel mid-resume releases the gate and drains a follow-up", async () => {
    // Scenario: user resolves approval → resumeExpectedRef is armed →
    // user hits Stop before the continuation emits. Cancel is now
    // server-reconciled: cancelMessage sends agent_abort but does NOT
    // locally clear the gate. The server then reconciles the aborted run
    // (run_ended outcome "aborted"), which force-releases the gate and
    // drains any queued follow-up. A send issued while the gate is still
    // armed (between Stop and the server terminal) must QUEUE, then fire
    // when the server reconciles.
    approvalQueue = [fakeApproval("agent-X", "ap-cancel")];
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
    await act(async () => {
      notifyRemoval("ap-cancel", "resolved");
      rerender();
    });

    // User hits Stop before the continuation arrives. The gate stays armed
    // (clientRunIdRef === r1) until the server reconciles.
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({ type: "agent_abort", agent_id: "agent-X" }),
    );

    // A follow-up typed before the server terminal must QUEUE, not dispatch.
    await act(async () => {
      await result.current.sendMessage("second");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);

    // Server reconciles the aborted run → gate releases → queue drains.
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: r1,
        outcome: "aborted",
      } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("second");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("transfers the gate when the first fresh-run event is a tool_end (approved exec's result)", async () => {
    // When the approved command was the only thing left, OpenClaw's
    // continuation may emit the tool_end / tool_error result directly
    // (backend transforms it via connection_pool.py) before any chunk.
    // The transfer must handle that path or the gate sticks armed
    // forever — the terminal done's runId never matches the pre-
    // approval clientRunIdRef.
    approvalQueue = [fakeApproval("agent-X", "ap-tool")];
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(() =>
      useAgentChat("agent-X", "session-1"),
    );

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const r1 = lastClientRunId();
    // Tool started on r1, then chat.final fires waiting for approval.
    act(() =>
      dispatch({
        type: "tool_start",
        agent_id: "agent-X",
        runId: r1,
        tool: "exec",
        toolCallId: "tc-1",
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r1 } as ChatIncomingMessage),
    );
    // Resolve the approval. In real usage useExecApprovals' setQueue
    // re-renders the hook, syncing approvalQueueRef. The mock just
    // mutates a variable, so we rerender to mirror that propagation —
    // without this, the next done would see the stale approval in
    // approvalQueueRef.current and refuse to release the gate.
    await act(async () => {
      notifyRemoval("ap-tool", "resolved");
      rerender();
    });

    // First fresh-run event is the tool's result, NOT a chunk.
    const r2 = "fresh-resume-runid";
    act(() =>
      dispatch({
        type: "tool_end",
        agent_id: "agent-X",
        runId: r2,
        tool: "exec",
        toolCallId: "tc-1",
        result: [{ type: "text", text: "ok" }],
      } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: r2 } as ChatIncomingMessage),
    );

    // Gate released via the transfer + matching done.
    await act(async () => {
      await result.current.sendMessage("after-resume");
    });
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("after-resume");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("keeps the gate armed when one approval is dismissed but siblings for this agent remain", async () => {
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

  it("clears isStreaming when releasing the gate on dismiss", async () => {
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

describe("useAgentChat — multi-tab observation (runId adoption + agent_busy rollback)", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    approvalQueue = [];
    removalHandlers.clear();
    vi.resetModules();
  });

  it("adopts an observed run's runId and ENQUEUES a new send instead of dispatching into the busy gate", async () => {
    // This tab did NOT start the run — it observes another tab's run via the
    // owner-keyed fan-out. Mirrors upstream handleChatEvent adoption
    // (controllers/chat.ts:711 / tui-event-handlers.ts:461): the observed runId
    // becomes the active run, so the tab knows it's busy and queues.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: "foreign-run",
        content: "working",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    // Submitting now ENQUEUES (adopted run is active) — no dispatch, so the
    // backend serialize gate is never hit.
    await act(async () => {
      await result.current.sendMessage("mine");
    });
    expect(sendChat).not.toHaveBeenCalled();
    expect(result.current.queuedMessages.map((m) => m.text)).toEqual(["mine"]);

    // The observed run ends → gate releases → the queued send drains.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: "foreign-run" } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0]?.[1]).toBe("mine");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("treats a stray agent_busy as a benign no-op (defensive — no server gate anymore)", async () => {
    // We no longer run a server-side serialize gate, so the backend never emits
    // agent_busy. But if a stray one arrives (mid-deploy skew), it must NOT tear
    // down the live run: it has no runId, and the generic error path would clear
    // clientRunIdRef + isStreaming.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("hi");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: r1, content: "live" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        code: "agent_busy",
        message: "busy",
      } as ChatIncomingMessage),
    );

    // Run untouched: still streaming, no error bubble, nothing queued.
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("drains the queue when a no-runId error releases the gate (no container / send failure)", async () => {
    // A send that fails BEFORE OpenClaw assigns a runId (no-container,
    // billing-gate, or chat.send failure) sends {type:error} with NO runId. It
    // releases the gate (clears clientRunIdRef) but there is no runId to key the
    // normal endedActiveRun drain on — and no trailing run_ended will arrive. A
    // follow-up the user queued while that send was pending must still drain.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("primary");
    });
    const r1 = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: r1, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // The active send fails with a no-runId transport/gate error.
    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        message: "No container provisioned. Please subscribe to start chatting.",
      } as ChatIncomingMessage),
    );

    // Gate released → the queued follow-up drains instead of stranding.
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
