import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
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

describe("useAgentChat — session-aware history key", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("fetches history for the General 3-part key by default", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(sendReq).toHaveBeenCalledWith(
        "chat.history",
        expect.objectContaining({ sessionKey: "agent:agent-X:user-1" }),
      );
    });
  });

  it("fetches history for the 4-part key after newSession()", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });
    await waitFor(() => {
      expect(sendReq).toHaveBeenCalledWith(
        "chat.history",
        expect.objectContaining({
          sessionKey: `agent:agent-X:user-1:${minted}`,
        }),
      );
    });
    expect(result.current.activeSessionId).toBe(minted);
  });
});
