/**
 * Server-signal queue draining for useAgentChat.
 *
 * The queue drains ONLY when the run is truly over per the SERVER —
 * never on a local terminal/error edge. The authoritative trigger is the
 * backend's `run_ended` frame (forwarded EXACTLY ONCE per run on a genuine
 * termination). A terminal `done`/`error` for the active runId with no
 * trailing `run_ended` (the session-less slash-command edge) also drains,
 * deduped by runId so `run_ended` + `done` for the same run never double-send.
 *
 * Mirrors upstream OpenClaw web UI's `flushChatQueue` model
 * (~/Desktop/openclaw/ui/src/ui/app-chat.ts:775) which drains only when
 * `!isChatBusy` and re-invokes when the run truly ends per the server.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

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

// No exec-approval provider mounted — the hook falls back to the empty
// default (queue: [], no-op onApprovalRemoved). These tests exercise the
// plain (non-approval) server-drain path.

function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — server-signal queue drain", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    vi.resetModules();
  });

  it("enqueues a follow-up while a run is active; it is NOT sent yet", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);

    // Run is in flight (a chunk has arrived) — a follow-up must queue.
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working...",
      } as ChatIncomingMessage),
    );

    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("follow-up");
  });

  it("a local `error` event does NOT drain the queue (the bug)", async () => {
    // The pre-fix behavior drained on the local clientRunId falling edge,
    // which an error toggled. The queue must drain ONLY on the server
    // run-ended signal — an error for the active run that carries no runId
    // matching the active run, OR carries a runId, is exercised separately;
    // here we assert a plain error for a *different* run never drains.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working...",
      } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // An error for an UNRELATED run must not drain the active run's queue.
    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        runId: "unrelated-run",
        message: "boom",
      } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);
  });

  it("`run_ended {runId:A}` drains exactly one queued message", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working...",
      } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // True terminal: backend forwards `done` then the authoritative
    // `run_ended`. Both carry runA.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: runA,
        outcome: "done",
      } as ChatIncomingMessage),
    );

    // Exactly one drain — the queued follow-up became the new active run.
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("a second `run_ended` for the same runId does not double-send", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });

    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);

    // A duplicate run_ended for runA must be ignored — the follow-up has
    // already become its own active run; re-draining would steal/dup it.
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
  });

  it("a terminal `done {runId:A}` with no run_ended also drains (session-less edge)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Session-less terminal: only `done`, NO trailing run_ended.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );

    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("run_ended after a terminal done for the SAME run does not double-drain", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("follow-up");
    });

    // Normal terminal sequence: done THEN run_ended for the same run.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );

    // The `done` drained one; the trailing run_ended must be a no-op for
    // runA (it has already been drained), so only ONE follow-up was sent.
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("follow-up");
  });

  it("drains queued messages one at a time across successive runs", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId: runA, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.sendMessage("q1");
    });
    await act(async () => {
      await result.current.sendMessage("q2");
    });
    expect(result.current.queuedMessages).toHaveLength(2);

    // End runA → drain q1 (becomes runB).
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("q1");
    expect(result.current.queuedMessages).toHaveLength(1);

    // End runB → drain q2 (becomes runC).
    const runB = lastClientRunId();
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runB, outcome: "done" } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(3);
    expect(sendChat.mock.calls[2]?.[1]).toBe("q2");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("an `agent_busy` error does NOT clear isStreaming or wipe the live run", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "live text",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    // The backend safety-net frame for a gated send — NO runId.
    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        code: "agent_busy",
        message: "Agent is still working on your previous message.",
      } as ChatIncomingMessage),
    );

    // Live turn untouched: still streaming, no "Error:" bubble, no error set.
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.error).toBeNull();
    const assistantMessages = result.current.renderItems.filter(
      (i) => i.kind === "message" && i.message.role === "assistant",
    );
    // Only the live stream bubble — no synthesized Error: message.
    const errorBubbles = assistantMessages.filter(
      (i) =>
        i.kind === "message" &&
        Array.isArray(i.message.content) &&
        i.message.content.some(
          (b) => b.type === "text" && (b.text ?? "").startsWith("Error:"),
        ),
    );
    expect(errorBubbles).toHaveLength(0);

    // The live run still ends cleanly on its own terminal sequence
    // (backend forwards `done` then the authoritative `run_ended`).
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );
    act(() =>
      dispatch({ type: "run_ended", agent_id: "agent-X", runId: runA, outcome: "done" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);
  });
});
