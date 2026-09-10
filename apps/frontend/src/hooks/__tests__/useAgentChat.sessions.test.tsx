import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
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
