/**
 * Cross-module smoke tests: for every file that was instrumented with a
 * product event, assert that taking the happy-path user action results in
 * `capture()` being called with the expected event name.
 *
 * These tests mock `@/lib/analytics` and assert on the mocked `capture`.
 * We don't assert on full property payloads here — that's covered by the
 * unit tests in analytics.test.ts for the helper itself, and the per-site
 * payload shape is load-bearing for the PostHog dashboard (wrong shape
 * shows up there). One smoke test per site = ~10 tests, not 30.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

// ---------------------------------------------------------------------------
// Shared analytics mock (hoisted)
// ---------------------------------------------------------------------------

const captureMock = vi.fn();
vi.mock("@/lib/analytics", () => ({
  capture: (...args: unknown[]) => captureMock(...args),
}));

// ---------------------------------------------------------------------------
// useAgents: agent_created + agent_deleted
// ---------------------------------------------------------------------------

const agentsRpcMock = vi.fn(async (method: string) => ({
  kind: "result" as const,
  value: method === "agents.list" ? { agents: [], defaultId: undefined } : {},
}));
const agentsRuntimeSource = {
  rpc: agentsRpcMock,
  getOperation: vi.fn(),
  onEvent: vi.fn(() => () => {}),
  connectionRevision: 0,
};

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => agentsRuntimeSource,
}));

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));

describe("instrumentation: useAgents", () => {
  beforeEach(() => {
    captureMock.mockReset();
    agentsRpcMock.mockClear();
  });

  it("fires agent_created after successful agents.create RPC", async () => {
    const { useAgents } = await import("@/hooks/useAgents");
    const { result } = renderHook(() => useAgents());
    await act(async () => {
      await result.current.createAgent({ name: "Marvin" });
    });
    // Find the agent_created call (RPC mock may produce other capture
    // calls indirectly — defensive filter).
    const call = captureMock.mock.calls.find((c) => c[0] === "agent_created");
    expect(call).toBeTruthy();
    expect(call?.[1]).toMatchObject({ agent_name: "Marvin" });
  });

  it("fires agent_deleted after successful agents.delete RPC", async () => {
    const { useAgents } = await import("@/hooks/useAgents");
    const { result } = renderHook(() => useAgents());
    await act(async () => {
      await result.current.deleteAgent("agent-123");
    });
    const call = captureMock.mock.calls.find((c) => c[0] === "agent_deleted");
    expect(call).toBeTruthy();
    expect(call?.[1]).toMatchObject({ agent_id: "agent-123" });
  });
});
