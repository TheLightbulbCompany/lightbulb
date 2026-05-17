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
