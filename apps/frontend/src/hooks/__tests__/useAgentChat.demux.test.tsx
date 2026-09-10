import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({ messages: [] });
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

function dispatch(msg: ChatIncomingMessage): void {
  chatHandlers.forEach((h) => h(msg));
}

// A first accepted chunk adopts the observed run and flips isStreaming on
// (useAgentChat:919-950). So on a clean hook — with no optimistic sendMessage
// priming the gate — isStreaming is the honest signal for "this frame was
// accepted": it stays false when the demux guard drops the frame and flips
// true only when the frame belongs to the active session.
describe("useAgentChat — event demux by session_id", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("drops a chunk stamped with a different session_id", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let sid = "";
    act(() => {
      sid = result.current.newSession();
    });
    const runId = crypto.randomUUID();

    // Frame for a DIFFERENT session (a background tab's run) must not paint —
    // dropped by the demux guard, so it never adopts the run nor streams.
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId,
        content: "leak",
        sessionId: "99999999-9999-4999-8999-999999999999",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);

    // Frame for the ACTIVE session is accepted and flips the stream on.
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId,
        content: "mine",
        sessionId: sid,
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);
  });

  it("drops an untagged General frame while a named session is active (General→named leak)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    act(() => {
      result.current.newSession();
    });
    const runId = crypto.randomUUID();

    // A legacy General run carries NO session_id. With a named session active,
    // a `"session_id" in msg` guard would fail-open and let this leak in; the
    // normalized guard drops it (undefined id == null != the active UUID).
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId,
        content: "general-leak",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);
  });

  it("General session (null) accepts frames with no session_id and drops 4-part frames", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    const runId = crypto.randomUUID();

    // No active session (General). A frame tagged for a real session must not
    // reach the General view.
    act(() =>
      dispatch({
        type: "chunk",
        agentId: "agent-X",
        runId,
        content: "other",
        sessionId: "11111111-1111-4111-8111-111111111111",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);

    // Legacy frame (no session_id) belongs to General → accepted.
    act(() =>
      dispatch({ type: "chunk", agentId: "agent-X", runId, content: "general" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);
  });
});
