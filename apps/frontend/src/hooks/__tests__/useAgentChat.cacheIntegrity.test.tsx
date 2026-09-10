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
const sendReq = vi.fn();
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

const GENERAL_KEY = "agent:agent-X:user-1";

function generalHistory(): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      { role: "user", content: [{ type: "text", text: "general question" }], timestamp: 1 },
      { role: "assistant", content: [{ type: "text", text: "general answer" }], timestamp: 2 },
    ],
  };
}

/** True when any rendered item carries the General transcript's text. */
function showsGeneralHistory(items: unknown[]): boolean {
  return JSON.stringify(items).includes("general answer");
}

function historyCallsFor(sessionKey: string): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === sessionKey,
  ).length;
}

describe("useAgentChat — per-session cache integrity on session switch", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    // Respond by sessionKey: General has history, everything else is empty.
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) return generalHistory();
      return { messages: [] };
    });
    vi.resetModules();
  });

  it("newSession() clears the transcript and fetches the 4-part key (old messages must not leak into the new session's cache)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // General history loads and renders.
    await waitFor(() => {
      expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
    });

    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });

    // The new session must actually query its own key — the cache write-back
    // effect must not have pre-populated (poisoned) it and short-circuited
    // the fetch.
    await waitFor(() => {
      expect(historyCallsFor(`agent:agent-X:user-1:${minted}`)).toBe(1);
    });
    // And the view must be a fresh transcript, not General's.
    expect(showsGeneralHistory(result.current.renderItems)).toBe(false);
    expect(result.current.renderItems).toHaveLength(0);
  });

  it("switching back to General restores its transcript from cache without refetching", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
    });

    act(() => {
      result.current.newSession();
    });
    await waitFor(() => {
      expect(result.current.renderItems).toHaveLength(0);
    });

    act(() => {
      result.current.selectSession(null);
    });
    // Restored from the module cache — instantly and without a second fetch.
    expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
    expect(historyCallsFor(GENERAL_KEY)).toBe(1);

    // And bouncing forward again still shows the empty new-session view.
    act(() => {
      result.current.newSession();
    });
    await waitFor(() => {
      expect(result.current.renderItems).toHaveLength(0);
    });
  });

  it("a history fetch that resolves after the user already switched sessions must not overwrite the current view", async () => {
    let resolveGeneral: (v: { messages: Array<Record<string, unknown>> }) => void = () => {};
    sendReq.mockReset().mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        return new Promise((res) => {
          resolveGeneral = res;
        });
      }
      return Promise.resolve({ messages: [] });
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // General fetch is in flight; switch to a new session before it resolves.
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(1);
    });
    act(() => {
      result.current.newSession();
    });

    // Late resolution for General: must cache under General's key, not stomp
    // the (empty) new-session transcript.
    await act(async () => {
      resolveGeneral(generalHistory());
      await Promise.resolve();
    });
    expect(result.current.renderItems).toHaveLength(0);

    // The cached late result still restores when switching back to General.
    act(() => {
      result.current.selectSession(null);
    });
    expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
  });

  it("a run finishing in a BACKGROUND session invalidates its cache so switching back refetches the reply", async () => {
    let generalFetches = 0;
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        generalFetches += 1;
        if (generalFetches >= 2) {
          const h = generalHistory();
          h.messages.push({
            role: "assistant",
            content: [{ type: "text", text: "background reply" }],
            timestamp: 3,
          });
          return h;
        }
        return generalHistory();
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
    });

    // Switch away; General's in-flight run then completes in the background.
    act(() => {
      result.current.newSession();
    });
    await waitFor(() => {
      expect(result.current.renderItems).toHaveLength(0);
    });
    act(() => {
      // Backend never tags General frames — an untagged done while a named
      // session is active is exactly the dropped-frame path.
      chatHandlers.forEach((h) => h({ type: "done", agentId: "agent-X", runId: "r-bg" }));
    });

    // Switching back must refetch (the cached copy predates the reply).
    act(() => {
      result.current.selectSession(null);
    });
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    await waitFor(() => {
      expect(JSON.stringify(result.current.renderItems)).toContain("background reply");
    });
  });

  it("surfaces historyTruncated when the backend trimmed the payload to fit the frame limit", async () => {
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        return { ...generalHistory(), truncated: true };
      }
      return { messages: [] };
    });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
    });
    expect(result.current.historyTruncated).toBe(true);

    // A session whose history was NOT trimmed reports false.
    act(() => {
      result.current.newSession();
    });
    await waitFor(() => {
      expect(result.current.renderItems).toHaveLength(0);
    });
    expect(result.current.historyTruncated).toBe(false);
  });

  it("sending a message does NOT clear historyTruncated — the older messages are still missing", async () => {
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        return { ...generalHistory(), truncated: true };
      }
      return { messages: [] };
    });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(result.current.historyTruncated).toBe(true);
    });
    await act(async () => {
      await result.current.sendMessage("hello again");
    });
    expect(result.current.historyTruncated).toBe(true);
  });

  it("a failed history fetch is retried on the next visit instead of latching the session blank", async () => {
    let failNext = true;
    sendReq.mockReset().mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        if (failNext) {
          failNext = false;
          throw new Error("gateway timeout");
        }
        return generalHistory();
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // First fetch fails — transcript is blank.
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(1);
    });
    expect(result.current.renderItems).toHaveLength(0);

    // Leave and come back: the fetch must be retried, not latched.
    act(() => {
      result.current.newSession();
    });
    act(() => {
      result.current.selectSession(null);
    });
    await waitFor(() => {
      expect(historyCallsFor(GENERAL_KEY)).toBe(2);
    });
    await waitFor(() => {
      expect(showsGeneralHistory(result.current.renderItems)).toBe(true);
    });
  });
});
