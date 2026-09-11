/**
 * F-RUN-ACTIVE: server-authoritative busy signal.
 *
 * The backend subscribes to OpenClaw's `sessions.changed` and forwards a
 * `run_active` frame whenever a run starts/stops on the container, giving
 * the client server truth independent of its own local `isStreaming`
 * anchor (which only reflects THIS tab's understanding of the stream).
 * `busy` ORs the two together — mirrors OpenClaw's own Control UI, which
 * derives busy as `chatRunId != null || session.hasActiveRun`.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({});
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useAgentRoomTransport", () => ({
  useAgentRoomTransport: (agentId: string | null) => ({
    isConnected: true,
    onChatMessage: (handler: (message: ChatIncomingMessage) => void) => {
      chatHandlers.push(handler);
      return () => {
        chatHandlers = chatHandlers.filter((candidate) => candidate !== handler);
      };
    },
    sendRoomMessage: async (message: string, clientMessageId: string) =>
      sendChat(agentId, message, clientMessageId, undefined),
    abortRun: async (runId: string) => {
      const accepted = send({ type: "agent_abort", agentId: agentId, runId });
      if (accepted === false) throw new Error("Stop not sent");
      return {};
    },
    steerRun: async (runId: string, text: string) => {
      const accepted = send({ type: "agent_steer", agentId: agentId, runId, text });
      if (accepted === false) throw new Error("Steer not sent");
      return {};
    },
    rpc: sendReq,
  }),
}));

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — run_active server-truth busy signal", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("run_active:true makes busy true with no stream events, and run_ended clears it", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => {
      dispatch({ type: "run_active", active: true, agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(true); // busy even though no chunk/tool arrived
    expect(result.current.isStreaming).toBe(false);

    act(() => {
      dispatch({
        type: "run_ended",
        runId: "r1",
        outcome: "done",
        agentId: "agentA",
      } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(false);
  });

  it("run_active for a DIFFERENT agent does not flip this hook's busy", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));
    act(() => {
      dispatch({ type: "run_active", active: true, agentId: "agentB" } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(false);
  });

  it("run_active:false clears busy directly (no terminal frame required)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => {
      dispatch({ type: "run_active", active: true, agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(true);

    act(() => {
      dispatch({ type: "run_active", active: false, agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(false);
  });

  it("switching session clears a latched busy (old scope's terminal is demux-dropped)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => {
      dispatch({ type: "run_active", active: true, agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(true);

    // Switch to a new session within the same agent — the old session's
    // run_active:false would carry the old session_id and be dropped, so the
    // switch itself must clear the server-truth bit.
    act(() => {
      result.current.newSession();
    });
    expect(result.current.busy).toBe(false);
  });

  it("clearMessages (agent switch path) clears a latched busy", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => {
      dispatch({ type: "run_active", active: true, agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.busy).toBe(true);

    act(() => {
      result.current.clearMessages();
    });
    expect(result.current.busy).toBe(false);
  });
});
