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

/** Transcript as it stood when the hook last saw it (before unmount). */
function historyWithoutReply(): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      { role: "user", content: [{ type: "text", text: "question before unmount" }], timestamp: 1 },
      { role: "assistant", content: [{ type: "text", text: "answer A" }], timestamp: 2 },
    ],
  };
}

/** Same transcript after a run finished server-side while the hook was unmounted. */
function historyWithReply(): { messages: Array<Record<string, unknown>> } {
  const h = historyWithoutReply();
  h.messages.push({
    role: "assistant",
    content: [{ type: "text", text: "answer B (finished while unmounted)" }],
    timestamp: 3,
  });
  return h;
}

function historyCallsFor(sessionKey: string): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === sessionKey,
  ).length;
}

describe("useAgentChat — history revalidates on mount (transcript-loss fix)", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) return historyWithoutReply();
      return { messages: [] };
    });
    vi.resetModules();
  });

  it("remount refetches history so a run that finished while unmounted appears", async () => {
    let generalFetches = 0;
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        generalFetches += 1;
        return generalFetches >= 2 ? historyWithReply() : historyWithoutReply();
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const first = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(JSON.stringify(first.result.current.renderItems)).toContain("answer A");
    });
    expect(historyCallsFor(GENERAL_KEY)).toBe(1);
    first.unmount();

    // While unmounted, a run completed server-side: history now holds answer B.
    const second = renderHook(() => useAgentChat("agent-X", "user-1"));
    // The cached transcript paints instantly...
    expect(JSON.stringify(second.result.current.renderItems)).toContain("answer A");
    // ...but the remount must still refetch once and pick up the new reply.
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    await waitFor(() => {
      expect(JSON.stringify(second.result.current.renderItems)).toContain(
        "answer B (finished while unmounted)",
      );
    });
  });

  it("remount with a cached transcript never flashes the loading state during the refetch", async () => {
    let resolveSecond: (v: { messages: Array<Record<string, unknown>> }) => void = () => {};
    let generalFetches = 0;
    sendReq.mockReset().mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        generalFetches += 1;
        if (generalFetches >= 2) {
          // Hold the remount's revalidation fetch open so we can observe the
          // in-flight state deterministically.
          return new Promise((res) => {
            resolveSecond = res;
          });
        }
        return Promise.resolve(historyWithoutReply());
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const first = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(JSON.stringify(first.result.current.renderItems)).toContain("answer A");
    });
    first.unmount();

    // Record isLoadingHistory on EVERY render of the second mount, so a
    // transient loading flash cannot hide between assertions.
    const loadingFlags: boolean[] = [];
    const second = renderHook(() => {
      const r = useAgentChat("agent-X", "user-1");
      loadingFlags.push(r.isLoadingHistory);
      return r;
    });

    // The background revalidation fetch is in flight...
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    // ...the cached transcript painted instantly...
    expect(JSON.stringify(second.result.current.renderItems)).toContain("answer A");
    // ...and no render has shown a loading state. Flush the microtask the
    // no-cache path uses to defer setHistoryLoadState("loading") — it must
    // not have been scheduled for a cached transcript.
    await act(async () => {
      await Promise.resolve();
    });
    expect(loadingFlags).not.toContain(true);

    await act(async () => {
      resolveSecond(historyWithReply());
      await Promise.resolve();
    });
    expect(JSON.stringify(second.result.current.renderItems)).toContain(
      "answer B (finished while unmounted)",
    );
    expect(loadingFlags).not.toContain(true);
  });
});
