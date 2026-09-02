import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const notifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyApiError: (...args: unknown[]) => notifyApiError(...args),
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

/** chat.history answers with `page`, everything else with `other`. */
function routeRpc(page: unknown, other: Record<string, unknown> = {}) {
  sendReq.mockImplementation(async (method: string) => {
    if (method === "chat.history") return page;
    if (method in other) return other[method];
    return {};
  });
}

const HISTORY_PAGE_1 = {
  messages: [
    { role: "user", content: "first", __openclaw: { id: "entry-1" } },
    { role: "assistant", content: "reply", __openclaw: { id: "entry-2" } },
  ],
};
const HISTORY_PAGE_2 = {
  messages: [{ role: "user", content: "only first survives", __openclaw: { id: "entry-1" } }],
};

describe("useAgentChat — rewind + branch switch", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset();
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    notifyApiError.mockReset();
    vi.resetModules();
  });

  it("rewind sends sessions.rewind for the active session key, refetches history, and returns editorText", async () => {
    routeRpc(HISTORY_PAGE_1, { "sessions.rewind": { editorText: "the removed prompt" } });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(2));

    // The refetch after the rewind must return the SHORTER transcript.
    routeRpc(HISTORY_PAGE_2, { "sessions.rewind": { editorText: "the removed prompt" } });

    let editorText: string | null = null;
    await act(async () => {
      editorText = await result.current.rewindToMessage("entry-1");
    });

    expect(editorText).toBe("the removed prompt");
    expect(sendReq).toHaveBeenCalledWith("sessions.rewind", {
      sessionKey: "agent:agent-X:user-1",
      entryId: "entry-1",
    });
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(1));
    expect(notifyApiError).not.toHaveBeenCalled();
  });

  it("rewind bumps branchRefreshToken so the branch list re-reads", async () => {
    routeRpc(HISTORY_PAGE_1, { "sessions.rewind": {} });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(2));
    const before = result.current.branchRefreshToken;

    await act(async () => {
      await result.current.rewindToMessage("entry-1");
    });

    expect(result.current.branchRefreshToken).toBeGreaterThan(before);
  });

  it("a rewind RPC failure surfaces via notifyApiError and leaves the transcript intact", async () => {
    routeRpc(HISTORY_PAGE_1);
    sendReq.mockImplementation(async (method: string) => {
      if (method === "chat.history") return HISTORY_PAGE_1;
      if (method === "sessions.rewind") throw new Error("session is busy");
      return {};
    });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(2));

    let editorText: string | null = "unset";
    await act(async () => {
      editorText = await result.current.rewindToMessage("entry-1");
    });

    expect(editorText).toBeNull();
    expect(notifyApiError).toHaveBeenCalled();
    expect(result.current.chatMessages).toHaveLength(2);
  });

  it("switchBranch sends sessions.branches.switch and refetches history", async () => {
    routeRpc(HISTORY_PAGE_1, { "sessions.branches.switch": {} });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(2));

    routeRpc(HISTORY_PAGE_2, { "sessions.branches.switch": {} });

    let ok = false;
    await act(async () => {
      ok = await result.current.switchBranch("leaf-9");
    });

    expect(ok).toBe(true);
    expect(sendReq).toHaveBeenCalledWith("sessions.branches.switch", {
      sessionKey: "agent:agent-X:user-1",
      leafEntryId: "leaf-9",
    });
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(1));
  });

  it("a switch failure surfaces via notifyApiError and returns false", async () => {
    sendReq.mockImplementation(async (method: string) => {
      if (method === "chat.history") return HISTORY_PAGE_1;
      if (method === "sessions.branches.switch") throw new Error("nope");
      return {};
    });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(2));

    let ok = true;
    await act(async () => {
      ok = await result.current.switchBranch("leaf-9");
    });

    expect(ok).toBe(false);
    expect(notifyApiError).toHaveBeenCalled();
    expect(result.current.chatMessages).toHaveLength(2);
  });

  it("targets the 4-part session key once a named session is active", async () => {
    routeRpc(HISTORY_PAGE_1, { "sessions.rewind": {} });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });
    await waitFor(() => expect(result.current.activeSessionId).toBe(minted));

    await act(async () => {
      await result.current.rewindToMessage("entry-1");
    });

    expect(sendReq).toHaveBeenCalledWith("sessions.rewind", {
      sessionKey: `agent:agent-X:user-1:${minted}`,
      entryId: "entry-1",
    });
  });
});
