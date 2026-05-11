import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "../useGateway";

// --- Mocks ---------------------------------------------------------------

// Chat-message handlers registered by useAgentChat.
let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(name: string, data: unknown) => void> = [];

const sendReq = vi.fn().mockResolvedValue({});
const sendChat = vi.fn();

vi.mock("../useGateway", () => ({
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

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn() },
}));

// Helpers to drive the hook from tests.
function emit(msg: ChatIncomingMessage) {
  act(() => {
    chatHandlers.forEach((h) => h(msg));
  });
}

function emitEvent(name: string, data: unknown) {
  act(() => {
    eventHandlers.forEach((h) => h(name, data));
  });
}

async function importHook() {
  const mod = await import("../useAgentChat");
  return mod.useAgentChat;
}

describe("useAgentChat — multi-bubble", () => {
  // Unique agent id per test: the hook has a module-level `_messageCache`
  // keyed by `${agentId}:${sessionName}` that persists across tests. Even
  // with `vi.resetModules()`, using a fresh id per test is a belt-and-braces
  // guarantee that no state leaks between cases.
  const nextAgent = (() => {
    let i = 0;
    return () => `agent-${++i}`;
  })();

  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    // Reset modules between tests so the module-level `_messageCache`
    // and `_needsBootstrap` inside useAgentChat don't leak state.
    // vi.mock() for useGateway hoists and is re-applied per module load.
    vi.resetModules();
  });

  it("creates one bubble per runId and mirrors cumulative chunk content", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    // Send a user message
    await act(async () => {
      await result.current.sendMessage("hello");
    });

    // First chunk for runId=R1 creates the bubble.
    emit({ type: "chunk", content: "Hi", agent_id: agentId, runId: "R1" });

    // Find the assistant message
    const assistants = result.current.messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].content).toBe("Hi");
    expect(result.current.isStreaming).toBe(true);

    // Second chunk — cumulative text for R1
    emit({ type: "chunk", content: "Hi there", agent_id: agentId, runId: "R1" });
    expect(result.current.messages.filter((m) => m.role === "assistant")[0].content).toBe("Hi there");

    // done finalizes
    emit({ type: "done", agent_id: agentId, runId: "R1" });
    expect(result.current.isStreaming).toBe(false);
  });

  it("renders two assistant bubbles when two runIds stream within one chat.send", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("do it");
    });

    // Run 1
    emit({ type: "chunk", content: "Let me try", agent_id: agentId, runId: "R1" });
    emit({ type: "done", agent_id: agentId, runId: "R1" });

    // Run 2 — different runId, should create a new bubble
    emit({ type: "chunk", content: "Done.", agent_id: agentId, runId: "R2" });
    emit({ type: "done", agent_id: agentId, runId: "R2" });

    const assistants = result.current.messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(2);
    expect(assistants[0].content).toBe("Let me try");
    expect(assistants[1].content).toBe("Done.");
    expect(result.current.isStreaming).toBe(false);
  });

  it("isStreaming stays true while any run is active (union of runs)", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("work");
    });

    emit({ type: "chunk", content: "a", agent_id: agentId, runId: "R1" });
    expect(result.current.isStreaming).toBe(true);

    // Second run starts before first finishes (interleaved deltas)
    emit({ type: "chunk", content: "b", agent_id: agentId, runId: "R2" });
    expect(result.current.isStreaming).toBe(true);

    emit({ type: "done", agent_id: agentId, runId: "R1" });
    // Still streaming — R2 still active
    expect(result.current.isStreaming).toBe(true);

    emit({ type: "done", agent_id: agentId, runId: "R2" });
    // All runs done — streaming false
    expect(result.current.isStreaming).toBe(false);
  });

  it("error with runId finalizes only that bubble; other runs continue", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("dual");
    });

    emit({ type: "chunk", content: "a", agent_id: agentId, runId: "R1" });
    emit({ type: "chunk", content: "b", agent_id: agentId, runId: "R2" });

    // Error on R1
    emit({ type: "error", message: "oh no", agent_id: agentId, runId: "R1" });

    const assistants = result.current.messages.filter((m) => m.role === "assistant");
    const r1 = assistants.find((m) => m.content.includes("Error:"));
    expect(r1).toBeTruthy();
    expect(result.current.isStreaming).toBe(true); // R2 still active

    emit({ type: "done", agent_id: agentId, runId: "R2" });
    expect(result.current.isStreaming).toBe(false);
  });

  it("error without runId clears all active runs", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("multi");
    });

    emit({ type: "chunk", content: "x", agent_id: agentId, runId: "R1" });
    emit({ type: "chunk", content: "y", agent_id: agentId, runId: "R2" });

    // Global error (no runId)
    emit({ type: "error", message: "global fail", agent_id: undefined });

    expect(result.current.isStreaming).toBe(false);
  });

  it("sendMessage does NOT create an assistant placeholder before first event", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("hi");
    });

    // Only a user message exists; no assistant placeholder yet. `runsRef` is
    // hook-internal, so we assert on the observable outcome: after
    // sendMessage resolves but before any chunk arrives, there should be
    // zero assistant messages.
    expect(
      result.current.messages.filter((m) => m.role === "assistant"),
    ).toHaveLength(0);
    expect(result.current.isStreaming).toBe(true); // sendMessage sets isStreaming = true early
  });

  it("post-approval chunks with new runId create a new bubble, not displace next user message's turn", async () => {
    // Regression test for the original bug this PR fixes: post-approval
    // stream chunks arriving after exec.approval.resolved were landing in
    // the NEXT user message's assistant bubble. With multi-bubble routing,
    // a distinct runId per turn should create a distinct assistant bubble.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    // --- Turn 1: user sends message #1, agent starts exec that needs approval
    await act(async () => {
      await result.current.sendMessage("install something");
    });

    // tool_start with runId "R1" — creates first assistant bubble
    emit({
      type: "tool_start",
      tool: "exec",
      toolCallId: "tc-1",
      agent_id: agentId,
      runId: "R1",
    });

    // Approval requested for the tool. R1 is still active (no done yet;
    // OpenClaw stalls the run until the approval resolves).
    emitEvent("exec.approval.requested", {
      id: "appr-1",
      request: {
        command: "rm -rf /tmp/foo",
        host: "gateway" as const,
        agentId: agentId,
        toolCallId: "tc-1",
        allowedDecisions: ["allow-once", "deny"],
      },
    });

    // Simulate approval resolution (does not finalize R1; the run continues).
    emitEvent("exec.approval.resolved", { id: "appr-1", decision: "allow-once" });

    // R1 finishes with a done
    emit({ type: "tool_end", tool: "exec", toolCallId: "tc-1", agent_id: agentId, runId: "R1" });
    emit({ type: "done", agent_id: agentId, runId: "R1" });

    // --- Turn 2: post-approval followup. OpenClaw assigns a NEW runId for
    // the follow-up stream (e.g. "exec-approval-followup:..."). Under the
    // fix this should create a SECOND assistant bubble, not reuse R1's.
    emit({
      type: "chunk",
      content: "Followup output",
      agent_id: agentId,
      runId: "exec-approval-followup:1",
    });
    emit({ type: "done", agent_id: agentId, runId: "exec-approval-followup:1" });

    const afterTurn2 = result.current.messages.filter((m) => m.role === "assistant");
    expect(afterTurn2).toHaveLength(2);
    // Turn 2 content is isolated to the second bubble.
    expect(afterTurn2[0].content).not.toContain("Followup output");
    expect(afterTurn2[1].content).toBe("Followup output");

    // --- Turn 3: user sends message #2 → new chat.send with runId "R3".
    await act(async () => {
      await result.current.sendMessage("next");
    });
    emit({ type: "chunk", content: "Third turn reply", agent_id: agentId, runId: "R3" });
    emit({ type: "done", agent_id: agentId, runId: "R3" });

    const finalAssistants = result.current.messages.filter((m) => m.role === "assistant");
    expect(finalAssistants).toHaveLength(3);

    // Critical assertion: turn 2's content did NOT get displaced into turn 3's bubble.
    expect(finalAssistants[2].content).toBe("Third turn reply");
    expect(finalAssistants[2].content).not.toContain("Followup output");
    // Each bubble has a distinct identity.
    const assistantIdsInternal = new Set(
      result.current.messages
        .filter((m) => m.role === "assistant")
        .map((m) => m.content),
    );
    expect(assistantIdsInternal.size).toBe(3);
  });

  it("late chunk after done does not create a ghost bubble or re-enable streaming", async () => {
    // Regression test for M1: after finalizeBubble(runId) deletes the run,
    // a trailing chunk for the same runId used to re-create a fresh empty
    // bubble and flip isStreaming back to true. The finalizedRunsRef guard
    // should silently drop such late events.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("hi");
    });

    emit({ type: "chunk", content: "Hello", agent_id: agentId, runId: "R1" });
    emit({ type: "done", agent_id: agentId, runId: "R1" });

    // After done: 1 assistant bubble, not streaming.
    expect(result.current.messages.filter((m) => m.role === "assistant")).toHaveLength(1);
    expect(result.current.isStreaming).toBe(false);

    // Late straggler chunk for the same finalized runId.
    emit({ type: "chunk", content: "Late content", agent_id: agentId, runId: "R1" });

    // Should NOT create a ghost bubble, NOT re-enable streaming, and should
    // NOT mutate the existing bubble's content.
    const assistants = result.current.messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].content).toBe("Hello");
    expect(result.current.isStreaming).toBe(false);

    // Also verify late thinking + tool_start are dropped.
    emit({ type: "thinking", content: "late thought", agent_id: agentId, runId: "R1" });
    emit({
      type: "tool_start",
      tool: "exec",
      toolCallId: "tc-late",
      agent_id: agentId,
      runId: "R1",
    });
    expect(result.current.messages.filter((m) => m.role === "assistant")).toHaveLength(1);
    expect(result.current.isStreaming).toBe(false);
  });

  it("scoped error with unknown runId clears isStreaming", async () => {
    // Regression test for Codex P1 / S3: sendMessage sets isStreaming=true
    // before any bubble exists. If a run errors before any chunk/thinking/
    // tool_start arrives, runsRef.get(runId) is undefined and the old
    // scoped-error branch only called setError() — leaving isStreaming
    // stuck at true and the input in "Stop" mode.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("boom");
    });

    // sendMessage flipped isStreaming=true, no bubble yet.
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.messages.filter((m) => m.role === "assistant")).toHaveLength(0);

    // Scoped error for a runId that was never bubbled.
    emit({
      type: "error",
      message: "provider failed",
      agent_id: agentId,
      runId: "R-never-bubbled",
    });

    // isStreaming must be cleared (otherwise input stays stuck).
    expect(result.current.isStreaming).toBe(false);
    // An error-marked assistant message was appended so the failure is visible.
    const assistants = result.current.messages.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].content).toContain("Error:");
    expect(result.current.error).toBeTruthy();
  });

  // Removed: "approval.requested event matches toolCallId across any assistant bubble".
  // The previous design glued exec approvals to tool-use bubbles inside
  // useAgentChat's `messages` state. That coupling was the source of the
  // orphan-pending-approval bug and the inline-card-in-wrong-bubble UX.
  // Approvals now live in a separate global queue (useExecApprovals) and
  // render as a modal at the chat-shell level. useAgentChat no longer
  // subscribes to exec.approval.* events at all, and ToolUse no longer
  // has a "pending-approval" status. Coverage for the new flow belongs in
  // useExecApprovals tests (follow-up).

  it("late events after cancelMessage do not resurrect a ghost bubble", async () => {
    // Regression test for Codex P2 (useAgentChat line ~912/930): cancelMessage
    // and clearMessages used to do `runsRef.clear(); finalizedRunsRef.clear()`,
    // which wiped the ghost-bubble guard for runs that were just aborted. A
    // late in-flight chunk/thinking/tool_start for one of those runIds would
    // then miss BOTH refs inside getOrCreateBubble, spawn a fresh empty
    // assistant bubble, and flip isStreaming back to true — silently undoing
    // the cancel. The fix migrates active runIds into finalizedRunsRef before
    // clearing runsRef so late stragglers are dropped.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("go");
    });

    // First chunk for R1 creates the bubble and keeps isStreaming=true.
    emit({ type: "chunk", content: "partial", agent_id: agentId, runId: "R1" });
    expect(result.current.isStreaming).toBe(true);
    expect(
      result.current.messages.filter((m) => m.role === "assistant"),
    ).toHaveLength(1);

    // User hits Stop.
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(result.current.isStreaming).toBe(false);
    const assistantCountAfterCancel = result.current.messages.filter(
      (m) => m.role === "assistant",
    ).length;

    // Late stragglers for R1 arrive after cancel (WebSocket reorder / OpenClaw
    // still flushing). These must NOT create a new bubble and must NOT flip
    // isStreaming back on.
    emit({ type: "chunk", content: "late chunk", agent_id: agentId, runId: "R1" });
    emit({ type: "thinking", content: "late thinking", agent_id: agentId, runId: "R1" });
    emit({
      type: "tool_start",
      tool: "exec",
      toolCallId: "tc-late",
      agent_id: agentId,
      runId: "R1",
    });

    const assistantCountAfterLate = result.current.messages.filter(
      (m) => m.role === "assistant",
    ).length;
    expect(assistantCountAfterLate).toBe(assistantCountAfterCancel);
    expect(result.current.isStreaming).toBe(false);
  });

  it("late events after cancel-before-first-event are tombstoned as ghost bubbles", async () => {
    // Regression test for Codex (useAgentChat line ~281): if the user hits
    // Stop BEFORE any chunk/thinking/tool_start for the in-flight turn has
    // arrived, runsRef is empty at cancel time — so finalizeAllActiveRuns
    // has no runIds to tombstone. A late first event then fell through to
    // getOrCreateBubble's bubble-creation path, spawning a ghost bubble and
    // flipping isStreaming back to true (silently undoing the cancel). The
    // pendingCancelRef flag closes that window.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    // User sends a message; sendMessage flips isStreaming=true but we never
    // emit any chunk/thinking/tool_start before cancel fires.
    await act(async () => {
      await result.current.sendMessage("go");
    });
    expect(result.current.isStreaming).toBe(true);
    expect(
      result.current.messages.filter((m) => m.role === "assistant"),
    ).toHaveLength(0);

    // User hits Stop before any event arrives — runsRef is empty here.
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(result.current.isStreaming).toBe(false);

    // Late first chunk arrives for a runId we've never seen. Must be dropped.
    emit({
      type: "chunk",
      content: "late straggler",
      agent_id: agentId,
      runId: "R-late",
    });

    expect(
      result.current.messages.filter((m) => m.role === "assistant"),
    ).toHaveLength(0);
    expect(result.current.isStreaming).toBe(false);
  });

  it("pendingCancelRef is cleared on new sendMessage so new runs are not dropped", async () => {
    // Companion test: verify the pendingCancelRef flag does not persist
    // past the next sendMessage. A new turn must allow events for its
    // new runId through normally.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    // Turn 1: send + cancel before any event arrives.
    await act(async () => {
      await result.current.sendMessage("first");
    });
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(result.current.isStreaming).toBe(false);

    // Turn 2: new sendMessage must reset pendingCancelRef so a normal chunk
    // for the new turn creates a bubble and flips isStreaming back on.
    await act(async () => {
      await result.current.sendMessage("second");
    });
    emit({
      type: "chunk",
      content: "normal reply",
      agent_id: agentId,
      runId: "R-fresh",
    });

    const assistants = result.current.messages.filter(
      (m) => m.role === "assistant",
    );
    expect(assistants).toHaveLength(1);
    expect(assistants[0].content).toBe("normal reply");
    expect(result.current.isStreaming).toBe(true);
  });
});
