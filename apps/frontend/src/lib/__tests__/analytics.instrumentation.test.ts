/**
 * Cross-module smoke tests: for every file that was instrumented with a
 * product event, assert that taking the happy-path user action results in
 * `capture()` being called with the expected event name.
 *
 * These tests mock `@/lib/analytics` and assert on the mocked `capture`.
 * We don't assert on full property payloads here — that's covered by the
 * unit tests in analytics.test.ts for the helper itself, and the per-site
 * payload shape is load-bearing for the PostHog dashboard (wrong shape
 * shows up there). One smoke test per site = ~10 tests, not 30.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

// ---------------------------------------------------------------------------
// Shared analytics mock (hoisted)
// ---------------------------------------------------------------------------

const captureMock = vi.fn();
vi.mock("@/lib/analytics", () => ({
  capture: (...args: unknown[]) => captureMock(...args),
}));

// ---------------------------------------------------------------------------
// useAgents: agent_created + agent_deleted
// ---------------------------------------------------------------------------

const rpcMutationMock = vi.fn().mockResolvedValue({});
const rpcMock = vi.fn(() => ({ data: { agents: [] }, error: null, isLoading: false, mutate: vi.fn() }));

vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => rpcMock(),
  useGatewayRpcMutation: () => rpcMutationMock,
}));

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));

describe("instrumentation: useAgents", () => {
  beforeEach(() => {
    captureMock.mockReset();
    rpcMutationMock.mockReset().mockResolvedValue({});
  });

  it("fires agent_created after successful agents.create RPC", async () => {
    const { useAgents } = await import("@/hooks/useAgents");
    const { result } = renderHook(() => useAgents());
    await act(async () => {
      await result.current.createAgent({ name: "Marvin" });
    });
    // Find the agent_created call (RPC mock may produce other capture
    // calls indirectly — defensive filter).
    const call = captureMock.mock.calls.find((c) => c[0] === "agent_created");
    expect(call).toBeTruthy();
    expect(call?.[1]).toMatchObject({ agent_name: "Marvin" });
  });

  it("fires agent_deleted after successful agents.delete RPC", async () => {
    const { useAgents } = await import("@/hooks/useAgents");
    const { result } = renderHook(() => useAgents());
    await act(async () => {
      await result.current.deleteAgent("agent-123");
    });
    const call = captureMock.mock.calls.find((c) => c[0] === "agent_deleted");
    expect(call).toBeTruthy();
    expect(call?.[1]).toMatchObject({ agent_id: "agent-123" });
  });
});

// ---------------------------------------------------------------------------
// useAgentChat: chat_message_sent, chat_completed, chat_aborted
// ---------------------------------------------------------------------------

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

describe("instrumentation: useAgentChat", () => {
  beforeEach(() => {
    captureMock.mockReset();
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    vi.resetModules();
  });

  it("fires chat_message_sent on sendMessage and chat_completed on done", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("hello world");
    });

    const sentCall = captureMock.mock.calls.find((c) => c[0] === "chat_message_sent");
    expect(sentCall).toBeTruthy();
    expect(sentCall?.[1]).toMatchObject({
      agent_id: "agent-X",
      message_length: "hello world".length,
    });

    // useAgentChat mints a clientRunId per sendMessage and gates the busy
    // state on it. The `done` handler only clears the busy gate (and
    // therefore emits chat_completed) when the incoming runId matches the
    // active clientRunId. We grab it from the sendChat mock — third arg.
    const clientRunId = sendChat.mock.calls[0]?.[2] as string;
    expect(typeof clientRunId).toBe("string");

    // Simulate the stream: a chunk to open the bubble, then `done`.
    act(() => {
      chatHandlers.forEach((h) =>
        h({
          type: "chunk",
          agent_id: "agent-X",
          runId: clientRunId,
          content: "hi back",
        } as ChatIncomingMessage),
      );
    });
    act(() => {
      chatHandlers.forEach((h) =>
        h({
          type: "done",
          agent_id: "agent-X",
          runId: clientRunId,
        } as ChatIncomingMessage),
      );
    });

    const completedCall = captureMock.mock.calls.find((c) => c[0] === "chat_completed");
    expect(completedCall).toBeTruthy();
    expect(completedCall?.[1]).toMatchObject({
      agent_id: "agent-X",
      assistant_message_length: "hi back".length,
    });
    expect(typeof completedCall?.[1].duration_ms).toBe("number");
  });

  it("does NOT fire chat_completed when a turn ends with a scoped error", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-Z", "session-1"));

    await act(async () => {
      await result.current.sendMessage("trigger error");
    });

    // Open the bubble with a chunk so the run state exists.
    act(() => {
      chatHandlers.forEach((h) =>
        h({
          type: "chunk",
          agent_id: "agent-Z",
          runId: "run-err",
          content: "partial",
        } as ChatIncomingMessage),
      );
    });

    // Now send a scoped error for that runId — finalizeBubble runs on
    // the error path, runsRef goes empty, but chat_completed must NOT fire
    // because the turn failed.
    act(() => {
      chatHandlers.forEach((h) =>
        h({
          type: "error",
          agent_id: "agent-Z",
          runId: "run-err",
          message: "model died",
        } as ChatIncomingMessage),
      );
    });

    const completedCall = captureMock.mock.calls.find((c) => c[0] === "chat_completed");
    expect(completedCall).toBeUndefined();
  });

  it("does NOT fire chat_completed when scoped error fires before any chunk (unknown-run branch)", async () => {
    // Error before first chunk → run not in runsRef → unknown-run branch
    // must also reset turn refs, otherwise a late `done` for a sibling
    // run would emit a bogus chat_completed.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-Z", "session-1"));

    await act(async () => {
      await result.current.sendMessage("trigger early error");
    });

    // Error fires for run-early WITHOUT a prior chunk — runsRef has no
    // entry for run-early, hits the unknown-run else branch.
    act(() => {
      chatHandlers.forEach((h) =>
        h({
          type: "error",
          agent_id: "agent-Z",
          runId: "run-early",
          message: "early failure",
        } as ChatIncomingMessage),
      );
    });

    // Now a stale `done` arrives for some other runId. With the fix, turn
    // refs were reset by the unknown-run branch, so chat_completed is gated
    // and does NOT fire.
    act(() => {
      chatHandlers.forEach((h) =>
        h({
          type: "done",
          agent_id: "agent-Z",
          runId: "run-stale",
        } as ChatIncomingMessage),
      );
    });

    const completedCall = captureMock.mock.calls.find((c) => c[0] === "chat_completed");
    expect(completedCall).toBeUndefined();
  });

  it("fires chat_aborted when cancelMessage runs during a streaming turn", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-Y", "session-1"));

    await act(async () => {
      await result.current.sendMessage("hi");
    });
    await act(async () => {
      await result.current.cancelMessage();
    });

    const call = captureMock.mock.calls.find((c) => c[0] === "chat_aborted");
    expect(call).toBeTruthy();
    expect(call?.[1]).toMatchObject({ agent_id: "agent-Y" });
    expect(typeof call?.[1].elapsed_ms).toBe("number");
  });
});
