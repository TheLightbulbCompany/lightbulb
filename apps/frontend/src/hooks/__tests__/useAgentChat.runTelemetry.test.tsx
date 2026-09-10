/**
 * Run telemetry: phase, elapsed clock, turn recap.
 *
 * The status strip under the transcript used to say "Working…" and nothing
 * else — identical at second 3 and second 300, so a long run and a hung run
 * looked the same. These three values are what let it tell them apart.
 *
 * The clock starts when the user SENDS, not when the first frame lands: the
 * gateway's run_status event carries `{phase}` and nothing else (no startedAt
 * on the wire), and send time is both earlier and more honest than "when this
 * tab first noticed". The consequence is that a run this client did not start
 * — an adopted/reconnected run — has no start time and shows no clock.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({});
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

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

/** The hook mints its own client run id on send, so pin it to address the
 *  terminal frames at the run the send actually started. */
function pinRunId(id: string) {
  return vi.spyOn(crypto, "randomUUID").mockReturnValue(id as `${string}-${string}-${string}-${string}-${string}`);
}

function phase(p: string, runId = "r1"): ChatIncomingMessage {
  return {
    type: "agent_activity",
    activity: "run_status",
    title: p,
    detail: { phase: p },
    runId,
    agentId: "agentA",
  } as ChatIncomingMessage;
}

describe("useAgentChat — run telemetry", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("records the latest run_status phase and clears it when the run ends", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => dispatch(phase("preparing_context")));
    expect(result.current.runPhase).toBe("preparing_context");

    act(() => dispatch(phase("starting_model")));
    expect(result.current.runPhase).toBe("starting_model");

    act(() => {
      dispatch({ type: "done", runId: "r1", agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.runPhase).toBeNull();
  });

  it("does not turn a run_status into a transcript pill", async () => {
    // A phase is the run's current state, not an event worth logging. A pill
    // reading "starting_model" would be both noise and a raw enum in the UI.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => dispatch(phase("starting_model")));
    expect(result.current.renderItems).toHaveLength(0);
  });

  it("maps compaction start and end onto the phase, not onto a pill", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => {
      dispatch({
        type: "agent_activity",
        activity: "compaction",
        title: "start",
        detail: { phase: "start" },
        runId: "r1",
        agentId: "agentA",
      } as ChatIncomingMessage);
    });
    expect(result.current.runPhase).toBe("compacting");
    expect(result.current.renderItems).toHaveLength(0);

    act(() => {
      dispatch({
        type: "agent_activity",
        activity: "compaction",
        title: "end",
        detail: { phase: "end", completed: true },
        runId: "r1",
        agentId: "agentA",
      } as ChatIncomingMessage);
    });
    expect(result.current.runPhase).toBeNull();
  });

  it("keeps a failed compaction visible rather than silently clearing", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => {
      dispatch({
        type: "agent_activity",
        activity: "compaction",
        title: "end",
        detail: { phase: "end", completed: false, willRetry: true },
        runId: "r1",
        agentId: "agentA",
      } as ChatIncomingMessage);
    });
    expect(result.current.runPhase).toBe("compaction_failed");
  });

  it("starts the clock on send, not on the first frame", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    expect(result.current.runStartedAt).toBeNull();
    await act(async () => {
      await result.current.sendMessage("go");
    });
    const started = result.current.runStartedAt;
    expect(started).not.toBeNull();

    // Later phases must not restart the clock.
    act(() => dispatch(phase("starting_model")));
    expect(result.current.runStartedAt).toBe(started);
  });

  it("leaves the clock unset for a run this client did not start", async () => {
    // Known limitation, stated deliberately: no run_status is re-emitted
    // mid-run, so a client that reconnects into a live run has no start time.
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    act(() => dispatch(phase("starting_model")));
    expect(result.current.runPhase).toBe("starting_model");
    expect(result.current.runStartedAt).toBeNull();
  });

  it("produces a turn recap when a run completes successfully", async () => {
    pinRunId("r1");
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    await act(async () => {
      await result.current.sendMessage("go");
    });
    act(() => {
      dispatch({ type: "done", runId: "r1", agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.lastTurnRecap).not.toBeNull();
    expect(result.current.lastTurnRecap!.runtimeMs).toBeGreaterThanOrEqual(0);
    expect(result.current.runStartedAt).toBeNull();
  });

  it("produces NO recap for a failed run", async () => {
    // A red error card with a cheerful "Done in 12s" underneath it is a
    // contradiction — the error surfaces own that outcome.
    pinRunId("r1");
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    await act(async () => {
      await result.current.sendMessage("go");
    });
    act(() => {
      dispatch({
        type: "error",
        message: "boom",
        runId: "r1",
        agentId: "agentA",
      } as ChatIncomingMessage);
    });
    expect(result.current.lastTurnRecap).toBeNull();
    expect(result.current.runPhase).toBeNull();
  });

  it("leaves no clock behind when the send itself throws", async () => {
    // dispatchNow stamps the clock BEFORE the wire send, and its catch rolls
    // back the optimistic turn. A stale timestamp is inert only while `busy`
    // stays false — but containerRunActive is independent server truth, so an
    // adopted run flipping it later would render a clock counting from a send
    // that never happened. That is the exact "no honest start time, no clock"
    // invariant every other terminal path preserves.
    send.mockReturnValue(true);
    sendChat.mockImplementation(() => {
      throw new Error("socket closed");
    });
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    await act(async () => {
      await result.current.sendMessage("go");
    });
    expect(result.current.runStartedAt).toBeNull();
    expect(result.current.runPhase).toBeNull();
  });

  it("clears the previous recap when the next run starts", async () => {
    const uuid = pinRunId("r1");
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    act(() => {
      dispatch({ type: "done", runId: "r1", agentId: "agentA" } as ChatIncomingMessage);
    });
    expect(result.current.lastTurnRecap).not.toBeNull();

    uuid.mockReturnValue("r2" as `${string}-${string}-${string}-${string}-${string}`);
    await act(async () => {
      await result.current.sendMessage("second");
    });
    expect(result.current.lastTurnRecap).toBeNull();
  });
});
