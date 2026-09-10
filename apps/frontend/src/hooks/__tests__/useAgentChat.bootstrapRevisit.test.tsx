/**
 * A bootstrap conclusion must never LATCH the session (useAgentChat).
 *
 * Observed on prod during the OpenClaw 8.1 walk: right after a gateway
 * reconnect one `chat.history` came back empty-but-200 while `sessions.list`
 * showed the session and a hand-issued `chat.history` returned its two
 * messages. The empty ladder concluded "bootstrap" and left the cacheKey in
 * `historyLoadedRef`, so re-selecting the session issued ZERO chat.history
 * calls (verified by instrumenting sendReq) — "Start a conversation" over a
 * real transcript for the rest of the page's life, until a full reload.
 *
 * The fix: `_needsBootstrap.has(cacheKey)` counts as NOT latched, so each
 * revisit re-reads once, and any non-empty read clears the flag. The amnesia
 * invariants stand: the re-read is silent (no skeleton over the bootstrap
 * card) and never blanks a cached transcript.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
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
const OTHER_SESSION = "11111111-2222-4333-8444-555555555555";

function history(text: string): { messages: Array<Record<string, unknown>> } {
  return {
    messages: [
      { role: "user", content: [{ type: "text", text: "q" }], timestamp: 1 },
      { role: "assistant", content: [{ type: "text", text }], timestamp: 2 },
    ],
  };
}

function generalHistoryCalls(): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === GENERAL_KEY,
  ).length;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Walk the full empty ladder (quick backoff → health probe → confirm) so the
 *  hook actually concludes bootstrap. Mirrors useAgentChat.emptyRetry. */
async function runEmptyLadder(): Promise<void> {
  await advance(0);
  await advance(500);
  await advance(1250);
  await advance(3000);
  await advance(0); // health probe microtask → confirm phase
  await advance(250);
  await advance(2000);
}

describe("useAgentChat — a bootstrap conclusion does not latch the session", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset();
    vi.useFakeTimers();
    vi.resetModules();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("re-reads history on a revisit after concluding bootstrap, and the second read's transcript wins", async () => {
    let generalReads = 0;
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey === GENERAL_KEY) {
        generalReads += 1;
        // Every read of the first visit is the transient empty-but-200; the
        // revisit's read returns what the server actually had all along.
        return generalReads <= 6 ? { messages: [] } : history("it was there all along");
      }
      // The session we park on mid-test resolves non-empty immediately, so
      // its own load doesn't leave the (shared, not per-key) history load
      // state on "loading" and muddy the silent-re-read assertions below.
      if (method === "chat.history") return history("other session");
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await runEmptyLadder();
    expect(generalHistoryCalls()).toBe(6);
    expect(result.current.needsBootstrap).toBe(true);
    // Silent re-read contract: the bootstrap card, not a skeleton.
    expect(result.current.isLoadingHistory).toBe(false);

    // Switch away to another session of the same agent, then back — exactly
    // the prod repro (a click in the session list, no reload).
    await act(async () => {
      result.current.selectSession(OTHER_SESSION);
    });
    await advance(0);
    const afterSwitchAway = generalHistoryCalls();

    await act(async () => {
      result.current.selectSession(null);
    });
    await advance(0);

    // THE regression: with the latch, the revisit issued no RPC at all.
    expect(generalHistoryCalls()).toBe(afterSwitchAway + 1);
    expect(JSON.stringify(result.current.renderItems)).toContain("it was there all along");
    expect(result.current.needsBootstrap).toBe(false);
    // The non-empty read cleared the flag, so the key is latched again — a
    // further revisit is an ordinary cached mount, not another re-read storm.
    expect(result.current.isLoadingHistory).toBe(false);
  });

  it("a genuinely empty session still bootstraps on the revisit, silently", async () => {
    sendReq.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
      if (method === "chat.history" && params?.sessionKey !== GENERAL_KEY) {
        return history("other session");
      }
      return { messages: [] };
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await runEmptyLadder();
    expect(result.current.needsBootstrap).toBe(true);

    await act(async () => {
      result.current.selectSession(OTHER_SESSION);
    });
    await advance(0);
    await act(async () => {
      result.current.selectSession(null);
    });
    // The revisit's re-read is in flight — the bootstrap card must stay up
    // rather than flipping to the loading skeleton.
    expect(result.current.isLoadingHistory).toBe(false);
    expect(result.current.needsBootstrap).toBe(true);

    await runEmptyLadder();
    expect(result.current.needsBootstrap).toBe(true);
    expect(result.current.isLoadingHistory).toBe(false);
  });
});
