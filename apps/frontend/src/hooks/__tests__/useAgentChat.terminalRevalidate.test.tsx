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

const SEED = {
  role: "assistant",
  content: [{ type: "text", text: "seed" }],
  timestamp: 0,
  __openclaw: { id: "entry-seed" },
};

/** The server's view once the turn is persisted: the user message now has the
 *  entryId `sessions.rewind` cuts at. Never SHORTER than the local transcript
 *  at terminal time (seed + optimistic user + folded assistant) — a shorter
 *  snapshot is the projection-lag case the shrink guard covers, exercised on
 *  its own below. */
function persistedTurn(): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      SEED,
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
        return reads === 1 ? { messages: [SEED] } : persistedTurn();
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
      dispatch({ type: "chunk", agentId: "agent-X", content: "hi back", runId } as ChatIncomingMessage);
      dispatch({ type: "done", agentId: "agent-X", runId } as ChatIncomingMessage);
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
      dispatch({ type: "done", agentId: "agent-X", runId } as ChatIncomingMessage);
      dispatch({ type: "run_ended", agentId: "agent-X", runId } as ChatIncomingMessage);
    });
    await waitFor(() => expect(historyCalls()).toBe(callsBefore + 1));
    // Settle: no second fetch from the trailing run_ended.
    await act(async () => {
      await Promise.resolve();
    });
    expect(historyCalls()).toBe(callsBefore + 1);
  });

  it("never SHRINKS the transcript on a soft refetch — a lagging projection can't erase the reply", async () => {
    // 8.1 serves chat.history from a projection. If the post-terminal read
    // lags the transcript write by a beat it returns a SHORTER snapshot, and
    // applying it would delete the assistant turn that just streamed AND cache
    // the truncated view.
    let reads = 0;
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // Mount: the seed. Post-terminal: the projection has not caught up and
        // returns only the seed again — 1 message against a local 3.
        return { messages: [SEED] };
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(historyCalls()).toBe(1));

    await act(async () => {
      await result.current.sendMessage("hello there");
    });
    const runId = lastClientRunId();
    await act(async () => {
      dispatch({ type: "chunk", agentId: "agent-X", content: "hi back", runId } as ChatIncomingMessage);
      dispatch({ type: "done", agentId: "agent-X", runId } as ChatIncomingMessage);
    });
    const localAfterTerminal = result.current.chatMessages.length;
    expect(localAfterTerminal).toBe(3); // seed + user + folded assistant

    await waitFor(() => expect(reads).toBeGreaterThan(1));
    await act(async () => {
      await Promise.resolve();
    });

    // The short snapshot was refused: the streamed reply is still on screen.
    expect(result.current.chatMessages).toHaveLength(3);
    expect(JSON.stringify(result.current.renderItems)).toContain("hi back");
  });

  it("applies a soft refetch that is the same length or longer", async () => {
    let reads = 0;
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        reads += 1;
        // Post-terminal read matches the local length exactly (3) — the normal
        // case, and the boundary the guard must NOT refuse.
        return reads === 1 ? { messages: [SEED] } : persistedTurn();
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => expect(historyCalls()).toBe(1));

    await act(async () => {
      await result.current.sendMessage("hello there");
    });
    const runId = lastClientRunId();
    await act(async () => {
      dispatch({ type: "chunk", agentId: "agent-X", content: "hi back", runId } as ChatIncomingMessage);
      dispatch({ type: "done", agentId: "agent-X", runId } as ChatIncomingMessage);
    });
    expect(result.current.chatMessages).toHaveLength(3);

    await waitFor(() => {
      const user = result.current.chatMessages.find((m) => m.role === "user");
      expect(user?.__openclaw?.id).toBe("entry-user-1");
    });
    expect(result.current.chatMessages).toHaveLength(3);
  });
});
