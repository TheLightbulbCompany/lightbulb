import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "../useGateway";

// --- Mocks ---------------------------------------------------------------
//
// Mirrors the mock shape in `useAgentChat.test.ts` (sibling file) so the
// queue/clientRunId state-machine tests share the same hand-rolled fake
// of the GatewayProvider. Only `sendChat` / `onChatMessage` / `isConnected`
// are used by the assertions below; the rest are stubbed so renderHook
// doesn't blow up calling them.

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

// `useAgentChat` calls `capture()` from `@/lib/analytics` inside
// `dispatchNow`. The real module reaches into the posthog SDK on the
// first call, which throws in jsdom. Stub posthog-js the same way the
// sibling test file does so the seam stays mockable here too.
vi.mock("posthog-js", () => ({
  default: { capture: vi.fn() },
}));

// Helper to drive a chat message into the hook. Wrapped in `act` so the
// resulting setState calls flush before the next assertion.
function emit(msg: ChatIncomingMessage) {
  act(() => {
    chatHandlers.forEach((h) => h(msg));
  });
}

async function importHook() {
  const mod = await import("../useAgentChat");
  return mod.useAgentChat;
}

describe("useAgentChat — queue + clientRunId state machine", () => {
  // Unique agent id per test: the hook has a module-level `_messageCache`
  // keyed by `${agentId}:${sessionName}` that persists across tests. A
  // fresh id per test prevents bubble/state leakage between cases.
  const nextAgent = (() => {
    let i = 0;
    return () => `q-agent-${++i}`;
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

  it("first sendMessage fires sendChat with a fresh clientRunId (UUID v4 shape)", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("hello");
    });

    expect(sendChat).toHaveBeenCalledTimes(1);
    const [calledAgentId, message, clientRunId] = sendChat.mock.calls[0];
    expect(calledAgentId).toBe(agentId);
    expect(message).toBe("hello");
    expect(typeof clientRunId).toBe("string");
    // crypto.randomUUID() shape — 36-char lowercase hex with 4 hyphens.
    expect(clientRunId).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("second sendMessage during a turn enqueues instead of firing", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    // Emit a chunk for the first turn — turn is now visibly in flight.
    emit({ type: "chunk", content: "hi", agent_id: agentId, runId: firstRunId });

    // Second send arrives mid-turn — must enqueue, not call sendChat again.
    await act(async () => {
      await result.current.sendMessage("second");
    });

    expect(sendChat).toHaveBeenCalledTimes(1); // still just the first
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("second");
  });

  it("on matching `done`, drains the queue and fires sendChat for the next item", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    emit({ type: "chunk", content: "hi", agent_id: agentId, runId: firstRunId });
    await act(async () => {
      await result.current.sendMessage("second");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Matching `done` for the first turn — clears busy gate and triggers
    // the flush effect to dispatch the next queued message.
    emit({ type: "done", agent_id: agentId, runId: firstRunId });

    expect(result.current.queuedMessages).toHaveLength(0);
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1][1]).toBe("second");
    // The second send should have a DIFFERENT clientRunId.
    expect(sendChat.mock.calls[1][2]).not.toBe(firstRunId);
    expect(typeof sendChat.mock.calls[1][2]).toBe("string");
    expect(sendChat.mock.calls[1][2]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("non-matching `done` (sub-run) does NOT drain the queue", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    emit({ type: "chunk", content: "hi", agent_id: agentId, runId: firstRunId });
    await act(async () => {
      await result.current.sendMessage("second");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // A sub-run finalizes with a DIFFERENT runId. The user-turn busy
    // flag (clientRunIdRef) must remain set — this is the bug we're
    // fixing: sub-run dones should not drain the queue.
    emit({
      type: "done",
      agent_id: agentId,
      runId: "sub-run-id-not-the-clientRunId",
    });

    expect(result.current.queuedMessages).toHaveLength(1); // still queued
    expect(sendChat).toHaveBeenCalledTimes(1); // no second dispatch
  });

  it("removeQueuedMessage drops a specific queue item", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    emit({ type: "chunk", content: "hi", agent_id: agentId, runId: firstRunId });
    await act(async () => {
      await result.current.sendMessage("queued-1");
    });
    await act(async () => {
      await result.current.sendMessage("queued-2");
    });
    expect(result.current.queuedMessages).toHaveLength(2);

    const idToRemove = result.current.queuedMessages[0].id;
    act(() => {
      result.current.removeQueuedMessage(idToRemove);
    });

    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("queued-2");
  });

  it("clearQueue empties the queue without affecting the active turn", async () => {
    const useAgentChat = await importHook();
    const agentId = nextAgent();
    const { result } = renderHook(() => useAgentChat(agentId, "main"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = sendChat.mock.calls[0][2] as string;

    emit({ type: "chunk", content: "hi", agent_id: agentId, runId: firstRunId });
    await act(async () => {
      await result.current.sendMessage("queued-1");
    });
    await act(async () => {
      await result.current.sendMessage("queued-2");
    });
    expect(result.current.queuedMessages).toHaveLength(2);

    act(() => {
      result.current.clearQueue();
    });

    expect(result.current.queuedMessages).toHaveLength(0);
    // First turn still streaming — sendChat call count unchanged.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.isStreaming).toBe(true);
  });
});
