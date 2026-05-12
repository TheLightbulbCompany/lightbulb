import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "../useGateway";

// --- Mocks ---------------------------------------------------------------
//
// Mirrors useAgentChat.queue.test.ts's setup. The approval flow uses the
// gateway's `onEvent` channel (NOT `onChatMessage`), so this file leans
// on the `eventHandlers` list more than the queue test does.

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

describe("useAgentChat — exec-approval busy-gate", () => {
  const nextAgent = (() => {
    let i = 0;
    return () => `appr-agent-${++i}`;
  })();

  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    vi.resetModules();
  });

  it("done event during pending approval does NOT clear the busy gate (new send still queues)", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const sessionName = "user-x";
    const { result } = renderHook(() => useAgentChat(agentId, sessionName));

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    // Agent stream activity, then an approval request arrives — the
    // event scoped to THIS session (agent + member id).
    emit({ type: "chunk", content: "thinking…", agent_id: agentId, runId: firstRunId });
    emitEvent("exec.approval.requested", {
      id: "approval-1",
      request: { sessionKey: `agent:${agentId}:${sessionName}`, command: "ls" },
    });

    // chat.final for the first run fires while the user is still
    // deciding. The busy gate MUST stay armed — without the approval-
    // pause guard the queue would open and concurrent sends would race
    // with the resumed run on the same session JSONL.
    emit({ type: "done", agent_id: agentId, runId: firstRunId });

    // A new send while waiting should queue, not fire.
    await act(async () => {
      await result.current.sendMessage("type while waiting");
    });

    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("type while waiting");
  });

  it("approval resolved + new run's first chunk swaps clientRunId; next done clears the gate", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const sessionName = "user-y";
    const { result } = renderHook(() => useAgentChat(agentId, sessionName));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    emit({ type: "chunk", content: "thinking", agent_id: agentId, runId: firstRunId });
    emitEvent("exec.approval.requested", {
      id: "appr-2",
      request: { sessionKey: `agent:${agentId}:${sessionName}`, command: "ls" },
    });
    emit({ type: "done", agent_id: agentId, runId: firstRunId });

    // Queue a follow-up while still mid-approval — the busy gate is armed.
    await act(async () => {
      await result.current.sendMessage("queued during approval");
    });
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(sendChat).toHaveBeenCalledTimes(1);

    // User clicks Allow — server sends exec.approval.resolved.
    emitEvent("exec.approval.resolved", { id: "appr-2", decision: "allow-once" });

    // Server starts the resumed run with a NEW runId (different from
    // the client-minted firstRunId). First chunk for that run triggers
    // the resume-swap inside getOrCreateBubble.
    const resumedRunId = "resumed-run-id-from-server";
    emit({ type: "chunk", content: "resumed!", agent_id: agentId, runId: resumedRunId });

    // The queue must STILL be holding the follow-up — chat.final for the
    // resumed run hasn't fired yet, gate is still armed (now tracking the
    // resumed runId via the swap).
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(sendChat).toHaveBeenCalledTimes(1);

    // chat.final for the resumed run — gate clears, queue drains, the
    // follow-up dispatches.
    emit({ type: "done", agent_id: agentId, runId: resumedRunId });

    expect(result.current.queuedMessages).toHaveLength(0);
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1][1]).toBe("queued during approval");
    // The follow-up's clientRunId is freshly minted, distinct from both
    // the original turn's id and the resumed-run id we faked above.
    const followupRunId = sendChat.mock.calls[1][2] as string;
    expect(followupRunId).not.toBe(firstRunId);
    expect(followupRunId).not.toBe(resumedRunId);
  });

  it("approval.requested for a DIFFERENT session is ignored (no busy-gate hijack)", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const sessionName = "user-z";
    const { result } = renderHook(() => useAgentChat(agentId, sessionName));

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;
    emit({ type: "chunk", content: "thinking…", agent_id: agentId, runId: firstRunId });

    // An approval request for a DIFFERENT user's session (org broadcast
    // leak guarded by useExecApprovals' isApprovalForCurrentUser). Our
    // pendingApprovals set must NOT pick it up.
    emitEvent("exec.approval.requested", {
      id: "appr-other",
      request: {
        sessionKey: `agent:${agentId}:some-other-member`,
        command: "ls",
      },
    });

    // chat.final arrives — without the foreign-session approval polluting
    // pendingApprovals, the gate clears as normal and the queue is open.
    emit({ type: "done", agent_id: agentId, runId: firstRunId });

    await act(async () => {
      await result.current.sendMessage("immediate next send");
    });

    // Should fire immediately (gate cleared correctly), not queue.
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("multi-step approvals: gate stays armed across multiple approve cycles", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const sessionName = "user-multi";
    const { result } = renderHook(() => useAgentChat(agentId, sessionName));

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;
    const expectedKey = `agent:${agentId}:${sessionName}`;
    emit({ type: "chunk", content: "thinking", agent_id: agentId, runId: firstRunId });

    // Two approvals requested in the same agent run.
    emitEvent("exec.approval.requested", {
      id: "appr-a",
      request: { sessionKey: expectedKey, command: "ls" },
    });
    emitEvent("exec.approval.requested", {
      id: "appr-b",
      request: { sessionKey: expectedKey, command: "pwd" },
    });
    emit({ type: "done", agent_id: agentId, runId: firstRunId });

    // Gate is armed; queue should accept a send.
    await act(async () => {
      await result.current.sendMessage("queued");
    });
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(sendChat).toHaveBeenCalledTimes(1);

    // Resolve only the FIRST approval — gate must stay armed because
    // appr-b is still pending. Resume-swap should NOT trigger.
    emitEvent("exec.approval.resolved", { id: "appr-a", decision: "allow-once" });

    const resumeRun1 = "resume-1";
    emit({ type: "chunk", content: "after-A", agent_id: agentId, runId: resumeRun1 });
    emit({ type: "done", agent_id: agentId, runId: resumeRun1 });

    // appr-b still pending → done for resumeRun1 must keep the gate armed
    // (it was NOT promoted to clientRunIdRef, because pendingApprovals
    // was non-empty when the resolve fired, so awaitingResume stayed false).
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(sendChat).toHaveBeenCalledTimes(1);

    // Resolve the second approval — NOW we await resume.
    emitEvent("exec.approval.resolved", { id: "appr-b", decision: "allow-once" });

    const resumeRun2 = "resume-2";
    emit({ type: "chunk", content: "after-B", agent_id: agentId, runId: resumeRun2 });
    emit({ type: "done", agent_id: agentId, runId: resumeRun2 });

    // Now the gate should be clear and the queued message should have drained.
    expect(result.current.queuedMessages).toHaveLength(0);
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1][1]).toBe("queued");
  });

  it("clearMessages during approval pause resets approval refs and lets a fresh send fire", async () => {
    // cancelMessage early-returns when isStreaming is false (the bubble
    // has already finalized via chat.final, even though the gate stays
    // armed for the pending approval). The real escape during an approval
    // pause from the user's POV is either deciding on the approval card
    // OR a /clear-style chat reset — we verify the latter here so the
    // ref-reset path in clearMessages is exercised.
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const sessionName = "user-clear";
    const { result } = renderHook(() => useAgentChat(agentId, sessionName));

    await act(async () => {
      await result.current.sendMessage("kick off");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;
    emit({ type: "chunk", content: "thinking", agent_id: agentId, runId: firstRunId });
    emitEvent("exec.approval.requested", {
      id: "appr-clear",
      request: { sessionKey: `agent:${agentId}:${sessionName}`, command: "ls" },
    });
    emit({ type: "done", agent_id: agentId, runId: firstRunId });

    await act(async () => {
      result.current.clearMessages();
    });

    // After clear, a new send should fire immediately — both the busy
    // gate and the stale approval refs must be reset.
    await act(async () => {
      await result.current.sendMessage("fresh start");
    });
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(result.current.queuedMessages).toHaveLength(0);
  });
});
