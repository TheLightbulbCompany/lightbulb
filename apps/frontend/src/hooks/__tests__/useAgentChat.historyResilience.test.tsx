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

const GENERAL_KEY = "agent:agent-X:user-1";

function historyWith(text: string): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      { role: "user", content: [{ type: "text", text: "q" }], timestamp: 1 },
      { role: "assistant", content: [{ type: "text", text }], timestamp: 2 },
    ],
  };
}

function historyCallsFor(sessionKey: string): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === sessionKey,
  ).length;
}

describe("useAgentChat — history-fetch resilience (#1125)", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset();
    vi.resetModules();
  });

  it("drops a stale in-flight fetch that resolves AFTER a same-key refetch superseded it", async () => {
    // Two concurrent fetches for the SAME session key. The first-issued one
    // resolves LAST with stale content (the pre-compact transcript); it must
    // not overwrite the fresh result the superseding fetch already applied.
    const generalResolvers: Array<(v: { messages: Array<Record<string, unknown>> }) => void> = [];
    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        return new Promise((res) => {
          generalResolvers.push(res);
        });
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // Fetch A (gen 1) is in flight for General.
    await waitFor(() => expect(historyCallsFor(GENERAL_KEY)).toBe(1));

    // Switch away and back before A resolves → issues fetch B (gen 2) for the
    // same key. (Same mechanism as /reset·/compact·/new invalidate-and-refetch.)
    act(() => {
      result.current.newSession();
    });
    act(() => {
      result.current.selectSession(null);
    });
    await waitFor(() => expect(historyCallsFor(GENERAL_KEY)).toBe(2));
    expect(generalResolvers).toHaveLength(2);

    // B (the fresh, superseding fetch) resolves first…
    await act(async () => {
      generalResolvers[1](historyWith("fresh answer"));
      await Promise.resolve();
    });
    // …then the stale A resolves last. It must be dropped.
    await act(async () => {
      generalResolvers[0](historyWith("stale answer"));
      await Promise.resolve();
    });

    const rendered = JSON.stringify(result.current.renderItems);
    expect(rendered).toContain("fresh answer");
    expect(rendered).not.toContain("stale answer");
  });
});
