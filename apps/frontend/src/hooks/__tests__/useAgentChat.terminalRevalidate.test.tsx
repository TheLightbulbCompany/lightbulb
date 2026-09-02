/**
 * Soft history revalidation on the run's terminal (useAgentChat).
 *
 * Two prod defects from the OpenClaw 8.1 walk, one cause:
 *
 *  - "Rewind to here" never appeared on anything typed in the current
 *    session. The optimistic user bubble `sendMessage` appends carries no
 *    `__openclaw.id`, MessageList gates the affordance on `persistedEntryId`,
 *    and nothing refetched history when the run ended — 0/7 bubbles offered
 *    it live, 7/7 after a reload.
 *  - The branch bar's active pill said "· 4 messages" for a branch that had
 *    6: SessionBranchBar re-lists only on `branchRefreshToken`, which only
 *    rewind/switch bumped.
 *
 * The terminal now schedules a SOFT revalidation — un-latch + nonce bump,
 * with `_messageCache`/`chatMessages` left alone — so the fetch effect takes
 * its silent cached-mount path and the server snapshot lands with its ids.
 */

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

const GENERAL_KEY = "agent:agent-X:user-1";

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}
function lastClientRunId(): string {
  return sendChat.mock.calls[sendChat.mock.calls.length - 1]?.[2] as string;
}
function historyCalls(): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === GENERAL_KEY,
  ).length;
}

/** The server's view once the turn is persisted: the user message now has the
 *  entryId `sessions.rewind` cuts at. */
function persistedTurn(): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      {
        role: "user",
        content: [{ type: "text", text: "hello there" }],
        timestamp: 1,
        __openclaw: { id: "entry-user-1" },
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "hi back" }],
        timestamp: 2,
        __openclaw: { id: "entry-asst-1" },
      },
    ],
  };
}

describe("useAgentChat — the run terminal soft-revalidates history", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendChat.mockReset().mockReturnValue(true);
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset();
    vi.resetModules();
  });

  it("issues a chat.history after `done`, folds in the entryIds, and never flips to loading", async () => {
    let reads = 0;
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // First read (mount) seeds a transcript so the session is not the
        // bootstrap case; the post-terminal read carries the persisted ids.
        return reads === 1
          ? { messages: [{ role: "assistant", content: [{ type: "text", text: "seed" }], timestamp: 0 }] }
          : persistedTurn();
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(historyCalls()).toBe(1));
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false));

    await act(async () => {
      await result.current.sendMessage("hello there");
    });
    const runId = lastClientRunId();
    // The optimistic bubble has no entryId — this is what killed the rewind
    // affordance for the whole session.
    const optimistic = result.current.chatMessages.find((m) => m.role === "user");
    expect(optimistic?.__openclaw?.id).toBeUndefined();

    const branchTokenBefore = result.current.branchRefreshToken;
    const callsBefore = historyCalls();

    await act(async () => {
      dispatch({ type: "chunk", agent_id: "agent-X", content: "hi back", runId } as ChatIncomingMessage);
      dispatch({ type: "done", agent_id: "agent-X", runId } as ChatIncomingMessage);
    });

    await waitFor(() => expect(historyCalls()).toBe(callsBefore + 1));
    // Silent: the transcript stays on screen through the refetch.
    expect(result.current.isLoadingHistory).toBe(false);

    await waitFor(() => {
      const user = result.current.chatMessages.find((m) => m.role === "user");
      expect(user?.__openclaw?.id).toBe("entry-user-1");
    });
    // Defect 4: the branch bar re-lists, so a new turn on a rewound branch
    // updates its message count.
    expect(result.current.branchRefreshToken).toBeGreaterThan(branchTokenBefore);
  });

  it("revalidates exactly once per run — the trailing `run_ended` is deduped", async () => {
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) return persistedTurn();
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(historyCalls()).toBe(1));

    await act(async () => {
      await result.current.sendMessage("hello there");
    });
    const runId = lastClientRunId();
    const callsBefore = historyCalls();

    await act(async () => {
      dispatch({ type: "done", agent_id: "agent-X", runId } as ChatIncomingMessage);
      dispatch({ type: "run_ended", agent_id: "agent-X", runId } as ChatIncomingMessage);
    });
    await waitFor(() => expect(historyCalls()).toBe(callsBefore + 1));
    // Settle: no second fetch from the trailing run_ended.
    await act(async () => {
      await Promise.resolve();
    });
    expect(historyCalls()).toBe(callsBefore + 1);
  });
});
