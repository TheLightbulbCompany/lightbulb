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
const sendReq = vi.fn();
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
    onEvent: () => () => {},
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

import { useAgentChat } from "../useAgentChat";

beforeEach(() => {
  chatHandlers = [];
  sendReq.mockReset();
  sendChat.mockReset();
  send.mockReset();
});

describe("useAgentChat history pagination", () => {
  it("requests a bounded newest page with an explicit offset", async () => {
    sendReq.mockResolvedValueOnce({
      messages: [{ role: "user", content: [{ type: "text", text: "hi" }] }],
      offset: 0,
      nextOffset: 1,
      hasMore: true,
      totalMessages: 160,
    });

    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));

    await waitFor(() => expect(sendReq).toHaveBeenCalled());
    const [method, params] = sendReq.mock.calls[0];
    expect(method).toBe("chat.history");
    expect(params.offset).toBe(0);
    expect(params.limit).toBe(30);
    await waitFor(() => expect(result.current.hasOlderMessages).toBe(true));
  });

  it("prepends an older page and advances the cursor", async () => {
    sendReq.mockResolvedValueOnce({
      messages: [{ role: "user", content: [{ type: "text", text: "newest" }] }],
      offset: 0,
      nextOffset: 1,
      hasMore: true,
      totalMessages: 2,
    });

    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(1));

    sendReq.mockResolvedValueOnce({
      messages: [{ role: "user", content: [{ type: "text", text: "older" }] }],
      offset: 1,
      nextOffset: 2,
      hasMore: false,
      totalMessages: 2,
    });

    await act(async () => {
      await result.current.loadOlderMessages();
    });

    const texts = result.current.chatMessages.map(
      (m) => (m.content as { text: string }[])[0].text,
    );
    expect(texts).toEqual(["older", "newest"]);
    expect(result.current.hasOlderMessages).toBe(false);
    expect(sendReq.mock.calls[1][1].offset).toBe(1);
  });
});
