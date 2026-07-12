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
// Mutable so a test can flicker the connection (isConnected false→true) to
// drive a second history-effect run and prove the in-flight dedupe latch.
let mockIsConnected = true;

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
    isConnected: mockIsConnected,
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
    mockIsConnected = true;
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

  it("a message sent during the in-flight revalidation survives it (view AND cache)", async () => {
    let resolveSecond: (v: { messages: Array<Record<string, unknown>> }) => void = () => {};
    let generalFetches = 0;
    sendReq.mockReset().mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        generalFetches += 1;
        if (generalFetches === 2) {
          // Hold the remount's revalidation open so a send can race it.
          return new Promise((res) => {
            resolveSecond = res;
          });
        }
        if (generalFetches >= 3) {
          // The third mount's revalidation stays pending forever: the cache
          // assertion below reads the transcript that paints synchronously
          // from the module cache, before any fetch could resolve.
          return new Promise(() => {});
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

    // Cached remount: the revalidation fetch is invisible (no loading state,
    // input enabled) so the user can send while it is in flight.
    const second = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    await act(async () => {
      await second.result.current.sendMessage("hello");
    });
    expect(JSON.stringify(second.result.current.renderItems)).toContain("hello");

    // The pre-send snapshot resolves late. It must NOT clobber the
    // optimistic send from the live view...
    await act(async () => {
      resolveSecond(historyWithoutReply());
      await Promise.resolve();
    });
    const view = JSON.stringify(second.result.current.renderItems);
    expect(view).toContain("hello");
    expect(view).toContain("answer A");

    // ...nor from the module cache: a fresh mount paints the cached
    // transcript synchronously, and it must still hold the sent message.
    second.unmount();
    const third = renderHook(() => useAgentChat("agent-X", "user-1"));
    expect(JSON.stringify(third.result.current.renderItems)).toContain("hello");
    third.unmount();
  });

  it("dedupes in-flight fetches: two rapid effect runs make ONE chat.history call", async () => {
    sendReq.mockReset().mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        // Never resolves: the fetch stays in flight across the flicker, so the
        // dedupe latch (set BEFORE sendReq) is the only thing that can keep a
        // second effect run from re-firing chat.history.
        return new Promise<{ messages: Array<Record<string, unknown>> }>(() => {});
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    mockIsConnected = true;
    const { rerender } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(1);
    });

    // isConnected flickers false→true while the first fetch is still pending.
    // Latching only in `.then` would leave the marker unset here and re-fire
    // the RPC; latching before sendReq makes the second run early-return.
    act(() => {
      mockIsConnected = false;
      rerender();
    });
    act(() => {
      mockIsConnected = true;
      rerender();
    });

    await act(async () => {
      await Promise.resolve();
    });
    expect(historyCallsFor(GENERAL_KEY)).toBe(1);
  });

  it("a failed history fetch un-latches so the next effect run retries", async () => {
    let calls = 0;
    sendReq.mockReset().mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        calls += 1;
        if (calls === 1) return Promise.reject(new Error("gateway drop"));
        return Promise.resolve(historyWithReply());
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    mockIsConnected = true;
    const { rerender, result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // First fetch rejects; its `.catch` must un-latch the loaded marker so a
    // later run can retry (a failed fetch can't leave the session blank).
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(1);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // A reconnect flicker triggers the retry.
    act(() => {
      mockIsConnected = false;
      rerender();
    });
    act(() => {
      mockIsConnected = true;
      rerender();
    });

    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    await waitFor(() => {
      expect(JSON.stringify(result.current.renderItems)).toContain(
        "answer B (finished while unmounted)",
      );
    });
  });

  it("keeps a cached transcript when the server returns empty (no purge, no bootstrap regression)", async () => {
    // An empty-but-200 chat.history read is NOT an authoritative "this
    // session was cleared" signal — a container mid-restart/update or a
    // not-yet-loaded session store returns exactly this. A cached transcript
    // must survive it: no cache purge, no live-view clear, no bootstrap flag.
    let generalFetches = 0;
    sendReq.mockReset().mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        generalFetches += 1;
        // First mount seeds the cache; every later mount sees a transient
        // empty server read.
        return generalFetches >= 2
          ? Promise.resolve({ messages: [] })
          : Promise.resolve(historyWithoutReply());
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const first = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(JSON.stringify(first.result.current.renderItems)).toContain("answer A");
    });
    first.unmount();

    const second = renderHook(() => useAgentChat("agent-X", "user-1"));
    // The cached transcript paints instantly...
    expect(JSON.stringify(second.result.current.renderItems)).toContain("answer A");
    // ...and the empty revalidation resolving must not clobber it.
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(JSON.stringify(second.result.current.renderItems)).toContain("answer A");
    expect(second.result.current.needsBootstrap).toBe(false);
    second.unmount();

    // The cache entry survives, so a fresh mount still paints it.
    const third = renderHook(() => useAgentChat("agent-X", "user-1"));
    expect(JSON.stringify(third.result.current.renderItems)).toContain("answer A");
    third.unmount();
  });
});
