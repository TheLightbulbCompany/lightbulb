import { describe, it, expect, vi, beforeEach, afterAll } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";

// Node's built-in localStorage needs a flag vitest doesn't pass (see
// useActiveSession.test.tsx), so install the shared Map-backed polyfill.
const restoreStorage = installStoragePolyfill();
afterAll(restoreStorage);

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

// Per-test module isolation (matches useAgentChat.cacheIntegrity.test.tsx):
// resetModules() gives every test a fresh `_messageCache` / `_historyPaging` /
// `_needsBootstrap` module store, and a dynamic import re-pulls the hook
// against that fresh instance. localStorage is ALSO cleared: useActiveSession
// backs its module-level `_activeByAgent` store with localStorage (a real
// jsdom API untouched by resetModules), and several tests below reuse the
// same "agent-1" id, so a session minted by an earlier test would otherwise
// leak in as the "current" session on a later test's fresh render.
beforeEach(() => {
  chatHandlers = [];
  sendReq.mockReset();
  sendChat.mockReset();
  send.mockReset();
  vi.resetModules();
  window.localStorage.clear();
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

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));

    await waitFor(() => expect(sendReq).toHaveBeenCalled());
    const [method, params] = sendReq.mock.calls[0];
    expect(method).toBe("chat.history");
    expect(params.offset).toBe(0);
    expect(params.limit).toBe(30);
    await waitFor(() => expect(result.current.hasOlderMessages).toBe(true));
  });

  it("prepends an older page and advances the cursor using the SERVER's nextOffset, not a local message count", async () => {
    // The raw first page has 4 messages, but one (an assistant "NO_REPLY")
    // is filtered out by projectChatHistoryMessages, so the VISIBLE page is
    // only 3 messages. nextOffset (7) is deliberately neither the raw count
    // (4) nor the projected count (3) — a wrong implementation that derived
    // the next offset from a local message count instead of trusting the
    // server's own value could not reproduce 7, so asserting on offset===7
    // below is what actually distinguishes the correct implementation.
    sendReq.mockResolvedValueOnce({
      messages: [
        { role: "assistant", content: [{ type: "text", text: "NO_REPLY" }] },
        { role: "user", content: [{ type: "text", text: "hello" }] },
        { role: "assistant", content: [{ type: "text", text: "hi there" }] },
        { role: "user", content: [{ type: "text", text: "newest" }] },
      ],
      offset: 0,
      nextOffset: 7,
      hasMore: true,
      totalMessages: 200,
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(3));

    sendReq.mockResolvedValueOnce({
      messages: [{ role: "user", content: [{ type: "text", text: "older" }] }],
      offset: 7,
      nextOffset: 8,
      hasMore: false,
    });

    await act(async () => {
      await result.current.loadOlderMessages();
    });

    // The second fetch must have used the server-reported nextOffset (7).
    expect(sendReq.mock.calls[1][1].offset).toBe(7);

    const texts = result.current.chatMessages.map(
      (m) => (m.content as { text: string }[])[0].text,
    );
    expect(texts).toEqual(["older", "hello", "hi there", "newest"]);
    expect(result.current.hasOlderMessages).toBe(false);
  });

  it("a page that resolves after the user switched sessions must not stomp the new session's transcript", async () => {
    const GENERAL_KEY = "agent:agent-1:user_x";
    let resolveOlder: (v: unknown) => void = () => {};

    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method !== "chat.history") return Promise.resolve({ messages: [] });
      const sessionKey = params?.sessionKey as string | undefined;
      const offset = params?.offset as number | undefined;
      if (sessionKey === GENERAL_KEY && offset === 0) {
        return Promise.resolve({
          messages: [{ role: "user", content: [{ type: "text", text: "newest" }] }],
          offset: 0,
          nextOffset: 1,
          hasMore: true,
        });
      }
      if (sessionKey === GENERAL_KEY && offset === 1) {
        // The older-page fetch triggered by loadOlderMessages() below — held
        // pending so it resolves AFTER the session switch.
        return new Promise((res) => {
          resolveOlder = res;
        });
      }
      // The new (minted) session's own initial history fetch.
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));

    await waitFor(() => expect(result.current.chatMessages).toHaveLength(1));
    await waitFor(() => expect(result.current.hasOlderMessages).toBe(true));

    act(() => {
      void result.current.loadOlderMessages();
    });
    await waitFor(() =>
      expect(
        sendReq.mock.calls.some(
          (c) => c[0] === "chat.history" && (c[1] as { offset?: number })?.offset === 1,
        ),
      ).toBe(true),
    );

    // Switch sessions WHILE the General older-page fetch is still pending.
    act(() => {
      result.current.newSession();
    });
    expect(result.current.chatMessages).toHaveLength(0);

    // Now resolve the stale General fetch.
    act(() => {
      resolveOlder({
        messages: [{ role: "user", content: [{ type: "text", text: "older" }] }],
        offset: 1,
        nextOffset: 2,
        hasMore: false,
      });
    });
    await waitFor(() => expect(result.current.isLoadingOlder).toBe(false));

    // The new (empty) session's transcript must NOT have been stomped by the
    // stale General page.
    expect(result.current.chatMessages).toHaveLength(0);

    // Switching back to General shows its own (unpolluted) transcript, and
    // the cursor was still advanced under General's key even though the live
    // view wasn't touched while it was inactive.
    act(() => {
      result.current.selectSession(null);
    });
    const texts = result.current.chatMessages.map(
      (m) => (m.content as { text: string }[])[0].text,
    );
    expect(texts).toEqual(["newest"]);
    expect(result.current.hasOlderMessages).toBe(false);
  });

  it("loadOlderMessages ignores a second synchronous invocation while a fetch is already in flight", async () => {
    let resolveOlder: (v: unknown) => void = () => {};

    sendReq.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method !== "chat.history") return Promise.resolve({ messages: [] });
      const offset = params?.offset as number | undefined;
      if (offset === 0) {
        return Promise.resolve({
          messages: [{ role: "user", content: [{ type: "text", text: "newest" }] }],
          offset: 0,
          nextOffset: 1,
          hasMore: true,
        });
      }
      // offset === 1 (the older page): held pending so both overlapping
      // invocations race against the same unresolved fetch.
      return new Promise((res) => {
        resolveOlder = res;
      });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));
    await waitFor(() => expect(result.current.chatMessages).toHaveLength(1));

    const callsBefore = sendReq.mock.calls.length;
    act(() => {
      // Two overlapping calls issued synchronously — neither has awaited yet,
      // so a state-based (rather than ref-based) guard would miss this.
      void result.current.loadOlderMessages();
      void result.current.loadOlderMessages();
    });

    const olderPageCalls = sendReq.mock.calls
      .slice(callsBefore)
      .filter((c) => c[0] === "chat.history" && (c[1] as { offset?: number })?.offset === 1);
    expect(olderPageCalls).toHaveLength(1);

    act(() => {
      resolveOlder({
        messages: [{ role: "user", content: [{ type: "text", text: "older" }] }],
        offset: 1,
        nextOffset: 2,
        hasMore: false,
      });
    });
    await waitFor(() => expect(result.current.isLoadingOlder).toBe(false));

    // Exactly one page was prepended — no duplicate.
    expect(result.current.chatMessages).toHaveLength(2);
  });

  it("clearMessages() drops the stale paging cursor along with the cache — /clear must not leave hasOlderMessages true on an empty transcript", async () => {
    sendReq.mockResolvedValueOnce({
      messages: [{ role: "user", content: [{ type: "text", text: "hi" }] }],
      offset: 0,
      nextOffset: 1,
      hasMore: true,
      totalMessages: 160,
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-1", "user_x", vi.fn()));
    await waitFor(() => expect(result.current.hasOlderMessages).toBe(true));

    const callsBeforeClear = sendReq.mock.calls.length;
    act(() => {
      result.current.clearMessages();
    });

    expect(result.current.chatMessages).toHaveLength(0);
    // The stale {nextOffset, hasMore:true} cursor must be gone, not just the
    // message cache — otherwise the empty transcript reports a phantom older
    // page.
    expect(result.current.hasOlderMessages).toBe(false);

    // And loadOlderMessages() must be a genuine no-op (no dangling cursor to
    // walk), not fire a request against a stale nextOffset.
    await act(async () => {
      await result.current.loadOlderMessages();
    });
    expect(sendReq.mock.calls.length).toBe(callsBeforeClear);
  });
});
