import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

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

vi.mock("@/hooks/useGateway", () => ({
  useGateway: (): {
    isConnected: boolean;
    nodeConnected: boolean;
    error: null;
    reconnectAttempt: number;
    send: typeof send;
    sendReq: typeof sendReq;
    sendChat: typeof sendChat;
    onEvent: (h: (n: string, d: unknown) => void) => () => void;
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => () => void;
    reconnect: () => void;
  } => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: () => () => {},
    onChatMessage: (h) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

function dispatch(msg: ChatIncomingMessage): void {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — session_id routing on write envelopes", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("General session sends NO session_id (backward compatible)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await act(async () => {
      await result.current.sendMessage("hi");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0][3]).toBeUndefined();
  });

  it("active session sends session_id on chat, abort, and steer", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let sid = "";
    act(() => {
      sid = result.current.newSession();
    });

    await act(async () => {
      await result.current.sendMessage("hello");
    });
    expect(sendChat.mock.calls[0][3]).toBe(sid);
    const runId = sendChat.mock.calls[0][2] as string;

    // Abort carries session_id.
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId, content: "x" } as ChatIncomingMessage),
    );
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({ type: "agent_abort", agent_id: "agent-X", session_id: sid }),
    );

    // Queue a second message, then steer it — carries session_id.
    await act(async () => {
      await result.current.sendMessage("steer me");
    });
    const qid = result.current.queuedMessages[0].id;
    act(() => {
      result.current.steerMessage(qid);
    });
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "agent_steer",
        agent_id: "agent-X",
        text: "steer me",
        session_id: sid,
      }),
    );
  });
});
