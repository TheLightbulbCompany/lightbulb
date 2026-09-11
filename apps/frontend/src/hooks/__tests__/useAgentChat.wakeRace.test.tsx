/**
 * The amnesia bug (prod, 2026-08-09): a returning free-tier owner's General
 * session — 127 stored messages — rendered the new-chat bootstrap, because
 * the empty-read ladder (4.75s of patience) raced a container wake measured
 * at 5m30s and concluded "empty" 4½ minutes before the session store came up.
 *
 * The fix gates the conclusion on reachability: an empty read from an
 * UNREACHABLE container (health probe rejects) says nothing, so the hook
 * keeps the loading skeleton and re-reads on a slow cadence until the
 * container answers. Only a reachable container's empty read — after grace
 * re-reads for store-lag — may conclude bootstrap.
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

function historyCalls(): number {
  return sendReq.mock.calls.filter(
    (c) => c[0] === "chat.history" && (c[1] as { sessionKey?: string })?.sessionKey === GENERAL_KEY,
  ).length;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

// Container simulator: while down, chat.history returns empty-but-200 and
// health REJECTS (pool can't reach the box). Once up, health resolves and
// history returns the stored transcript.
let containerUp = false;
const STORED = {
  messages: [
    { role: "user", content: [{ type: "text", text: "onboarding brief" }], timestamp: 1 },
    { role: "assistant", content: [{ type: "text", text: "kickoff done yesterday" }], timestamp: 2 },
  ],
};

function armContainerMock(): void {
  sendReq.mockImplementation((method: string) => {
    if (method === "health") {
      return containerUp ? Promise.resolve({ ok: true }) : Promise.reject(new Error("gateway unreachable"));
    }
    if (method === "chat.history") {
      return Promise.resolve(containerUp ? STORED : { messages: [] });
    }
    return Promise.resolve({});
  });
}

describe("useAgentChat — empty reads from an unreachable container never conclude bootstrap", () => {
  beforeEach(() => {
    chatHandlers = [];
    containerUp = false;
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    sendReq.mockReset();
    armContainerMock();
    vi.useFakeTimers();
    vi.resetModules();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("keeps the skeleton through a multi-minute wake and recovers the transcript when the container comes up", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    // Quick ladder runs and exhausts against the waking container.
    await advance(0);
    await advance(500);
    await advance(1250);
    await advance(3000);
    expect(historyCalls()).toBe(4);
    // Probe rejects (unreachable) — the OLD behaviour concluded bootstrap
    // here. Now: no conclusion, skeleton stays, slow cadence armed.
    await advance(0);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);

    // Three minutes of wake: reads keep ticking every 5s, still no bootstrap.
    await advance(180_000);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(true);
    const readsDuringWake = historyCalls();
    expect(readsDuringWake).toBeGreaterThan(4);

    // Container finishes waking. The next slow read still sees the store
    // mid-load (empty) but the probe now RESOLVES -> grace re-read recovers
    // the full transcript.
    containerUp = true;
    await advance(10_000);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.isLoadingHistory).toBe(false);
    expect(result.current.chatMessages.length).toBeGreaterThan(0);
  });

  it("switching away while a wake read is in flight un-latches instead of branding the old session bootstrap", async () => {
    // Codex P1: the in-flight read's empty resolution used to fall through to
    // _needsBootstrap.add for a session the user had already left — with the
    // latch retained, so returning to it skipped the refetch and painted the
    // new-chat bootstrap over a full transcript. The switch must instead
    // un-latch and conclude nothing.
    let resolvePending: ((v: unknown) => void) | null = null;
    let holdNextRead = false;
    sendReq.mockImplementation((method: string) => {
      if (method === "health") {
        return containerUp ? Promise.resolve({ ok: true }) : Promise.reject(new Error("unreachable"));
      }
      if (method === "chat.history") {
        if (holdNextRead) {
          holdNextRead = false;
          return new Promise((res) => {
            resolvePending = res;
          });
        }
        return Promise.resolve(containerUp ? STORED : { messages: [] });
      }
      return Promise.resolve({});
    });

    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result, rerender } = renderHook(
      ({ agent }: { agent: string }) => useAgentChat(agent, "user-1"),
      { initialProps: { agent: "agent-X" } },
    );

    // Quick ladder exhausts, wake phase arms.
    await advance(0);
    await advance(500);
    await advance(1250);
    await advance(3000);
    await advance(0);

    // Next wake round: hold the read in flight, THEN switch agents. No timer
    // is pending while the read is in flight, so the effect cleanup keeps the
    // latch — the resolution callback is the only thing that can release it.
    holdNextRead = true;
    await advance(5_000); // fires the held read
    await act(async () => {
      rerender({ agent: "agent-Y" });
    });
    await act(async () => {
      resolvePending?.({ messages: [] });
    });
    await advance(0);

    // Return to the original agent: the container is up now, and the revisit
    // must REFETCH (latch released) rather than render bootstrap.
    containerUp = true;
    await act(async () => {
      rerender({ agent: "agent-X" });
    });
    await advance(1_000);
    expect(result.current.needsBootstrap).toBe(false);
    expect(result.current.chatMessages.length).toBeGreaterThan(0);
  });

  it("a real unmount after a StrictMode-style re-mount stops the wake chain", async () => {
    // Codex P2: the latched re-mount used to early-return WITHOUT registering
    // a cleanup, so the final unmount had nothing to call — the mounted ref
    // stayed true and the wake chain kept firing RPCs from a dead hook for
    // the full ceiling. Every effect run must register the cleanup.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { rerender, unmount } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await advance(0);
    await advance(500);
    // Force a latched re-run of the effect mid-ladder (isConnected flickers
    // rerender in the real app; a plain rerender exercises the same path).
    await act(async () => {
      rerender();
    });

    // Enter the wake phase, confirm the chain is alive.
    await advance(1250);
    await advance(3000);
    await advance(0);
    await advance(10_000);
    const beforeUnmount = historyCalls();
    expect(beforeUnmount).toBeGreaterThan(4);

    unmount();
    await advance(60_000);
    expect(historyCalls()).toBe(beforeUnmount);
  });

  it("gives up honestly at the wake ceiling instead of spinning forever", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));

    await advance(0);
    await advance(500);
    await advance(1250);
    await advance(3000);
    // 120 slow attempts x 5s ≈ 10 minutes, plus probe microtasks.
    await advance(700_000);
    expect(result.current.needsBootstrap).toBe(true);
    expect(result.current.isLoadingHistory).toBe(false);
    const settled = historyCalls();
    await advance(60_000);
    expect(historyCalls()).toBe(settled);
  });
});
