/**
 * T8: `/btw` (and `/side`) detached side-channel sender for useAgentChat.
 *
 * A `/btw …` message is a non-blocking aside that runs ALONGSIDE the main turn
 * as a SEPARATE run rather than queuing behind it. sendMessage detects the
 * prefix and:
 *   - MINTS the side run's runId and sends it as `clientRunId` (→ backend
 *     idempotencyKey → the runId every side event carries) alongside
 *     {type:"agent_chat", agent_id, message:<FULL text incl. prefix>, side:true},
 *     IMMEDIATELY, even while a primary run is active;
 *   - records that runId so the event handler segregates the side run;
 *   - does NOT enqueue it;
 *   - does NOT touch clientRunIdRef / isStreaming (a side run is detached).
 *
 * Segregation (the crucial part): a side run shares the sessionKey + agent_id
 * but carries its own runId, so its chunk/done/error/run_ended events MUST NOT
 * touch the primary turn's live state — otherwise a side chunk would hijack the
 * live stream and a side `done` would fold+clear the still-running primary turn.
 * Mirrors upstream OpenClaw web UI's sendDetachedChatMessage (mints the runId,
 * controllers/chat.ts) + handleChatEvent's activeRunMatches gating; the TUI
 * tracks btw run ids explicitly (noteLocalBtwRunId / isLocalBtwRunId).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

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

function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}

/** The runId minted for the most recent `/btw` (side) send. */
function lastSideRunId(): string {
  const calls = send.mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const p = calls[i]?.[0] as { side?: boolean; clientRunId?: string } | undefined;
    if (p?.side) return p.clientRunId as string;
  }
  return "";
}

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

function assistantText(result: { current: { renderItems: unknown[] } }): string {
  return result.current.renderItems
    .filter(
      (i): i is { kind: "message"; message: { role: string; content: unknown } } =>
        (i as { kind?: string }).kind === "message" &&
        (i as { message?: { role?: string } }).message?.role === "assistant",
    )
    .map((i) =>
      Array.isArray(i.message.content)
        ? (i.message.content as Array<{ type: string; text?: string }>)
            .filter((b) => b.type === "text")
            .map((b) => b.text ?? "")
            .join("")
        : "",
    )
    .join("\n");
}

describe("useAgentChat — T8 /btw side-channel", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset();
    vi.resetModules();
  });

  it("sends a /btw message as {side:true} with the prefix intact + a minted clientRunId, not via sendChat", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("/btw check the logs while you work");
    });

    // No primary run started, nothing queued.
    expect(sendChat).not.toHaveBeenCalled();
    expect(result.current.queuedMessages).toHaveLength(0);
    expect(result.current.isStreaming).toBe(false);

    // Sent as a detached side run, FULL text incl. the /btw prefix, with a
    // minted clientRunId (the side run's tracked runId).
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "agent_chat",
      agent_id: "agent-X",
      message: "/btw check the logs while you work",
      side: true,
      clientRunId: expect.any(String),
    });
    expect(lastSideRunId()).toBeTruthy();
  });

  it("also recognizes the /side alias", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("/side peek at the queue");
    });

    expect(send).toHaveBeenCalledWith({
      type: "agent_chat",
      agent_id: "agent-X",
      message: "/side peek at the queue",
      side: true,
      clientRunId: expect.any(String),
    });
    expect(sendChat).not.toHaveBeenCalled();
  });

  it("sends /btw IMMEDIATELY while a primary run is active — does NOT enqueue or disturb the primary run", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // Start a primary run.
    await act(async () => {
      await result.current.sendMessage("primary turn");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "working...",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    // /btw while busy: dispatched as a side run, NOT queued.
    await act(async () => {
      await result.current.sendMessage("/btw quick aside");
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({
      type: "agent_chat",
      agent_id: "agent-X",
      message: "/btw quick aside",
      side: true,
      clientRunId: expect.any(String),
    });
    // The primary run is undisturbed: still the only sendChat, still streaming,
    // nothing in the queue.
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(0);
    expect(result.current.isStreaming).toBe(true);
  });

  it("a side run's chunk does NOT hijack the primary live stream", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("primary turn");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "ALPHA primary text",
      } as ChatIncomingMessage),
    );

    // Fire a /btw side run, then stream a chunk on ITS runId.
    await act(async () => {
      await result.current.sendMessage("/btw side note");
    });
    const sideRunId = lastSideRunId();
    expect(sideRunId).toBeTruthy();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: sideRunId,
        content: "BETA side leak",
      } as ChatIncomingMessage),
    );

    // Primary turn still streaming; the side chunk did not switch the stream.
    expect(result.current.isStreaming).toBe(true);

    // End the primary run: the folded message must contain ONLY the primary
    // text — the side chunk must never have entered the primary stream.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );
    const text = assistantText(result);
    expect(text).toContain("ALPHA primary text");
    expect(text).not.toContain("BETA side leak");
    expect(result.current.isStreaming).toBe(false);
  });

  it("a side run's done does NOT fold or clear the primary turn", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("primary turn");
    });
    const runA = lastClientRunId();
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId: runA,
        content: "still working",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);

    await act(async () => {
      await result.current.sendMessage("/btw side note");
    });
    const sideRunId = lastSideRunId();

    // A side `done` arrives while the primary run is still in flight.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: sideRunId } as ChatIncomingMessage),
    );

    // The primary turn is NOT folded/cleared: still streaming, no history yet.
    expect(result.current.isStreaming).toBe(true);
    expect(assistantText(result)).not.toContain("still working"); // not folded into history

    // The PRIMARY done ends the turn normally.
    act(() =>
      dispatch({ type: "done", agent_id: "agent-X", runId: runA } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);
    expect(assistantText(result)).toContain("still working");
  });

  it("a side run_ended (tracked) is dropped and does NOT drain the primary queue", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    // Primary run in flight with a queued follow-up behind it.
    await act(async () => {
      await result.current.sendMessage("primary turn");
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
      await result.current.sendMessage("queued follow-up");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Fire a /btw side run; it shouldn't queue.
    await act(async () => {
      await result.current.sendMessage("/btw side note");
    });
    const sideRunId = lastSideRunId();
    expect(result.current.queuedMessages).toHaveLength(1);

    // The side run ends with its OWN (tracked) runId while the primary is
    // active. Segregation drops it, so it must NOT flush the queued follow-up.
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: sideRunId,
        outcome: "done",
      } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("queued follow-up");

    // When the PRIMARY run truly ends, the queue drains normally.
    act(() =>
      dispatch({
        type: "run_ended",
        agent_id: "agent-X",
        runId: runA,
        outcome: "done",
      } as ChatIncomingMessage),
    );
    expect(sendChat).toHaveBeenCalledTimes(2);
    expect(sendChat.mock.calls[1]?.[1]).toBe("queued follow-up");
    expect(result.current.queuedMessages).toHaveLength(0);
  });

  it("does not treat a non-prefix message containing 'btw' mid-text as a side run", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("by the way / btw is not a command here");
    });

    // Regular send — goes through sendChat, not the side channel.
    expect(send).not.toHaveBeenCalled();
    expect(sendChat).toHaveBeenCalledTimes(1);
  });
});
