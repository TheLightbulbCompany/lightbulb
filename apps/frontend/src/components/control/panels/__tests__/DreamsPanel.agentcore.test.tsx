/**
 * M6 task 10-D, row 18. Dreams (memory-core's nightly sweep/diary/phases) has
 * no Hermes equivalent — `hermes claw migrate` archives OpenClaw `memory.*`
 * for manual review rather than translating it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { DreamsPanel } from "../DreamsPanel";

let gate: "loading" | "hidden" | "shown";
const rpcMethods: (string | null)[] = [];
const patchConfigMock = vi.fn();

vi.mock("@lightbulb/shared", () => ({ useHiddenOnAgentCore: () => gate }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    rpcMethods.push(method);
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useRuntimeRpcMutation: () => vi.fn(),
}));
vi.mock("@/hooks/useConfigPatch", () => ({
  useConfigPatch: () => ({ config: {}, configReady: true, patchConfig: patchConfigMock, refreshConfig: vi.fn() }),
}));
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [], defaultId: undefined, error: undefined, isLoading: false }),
}));

describe("DreamsPanel on an AgentCore workspace", () => {
  beforeEach(() => {
    rpcMethods.length = 0;
    patchConfigMock.mockReset();
    gate = "hidden";
  });

  it("renders a short notice instead of the sweep/diary/phases editor", () => {
    render(<DreamsPanel />);
    expect(screen.getByText(/isn't available on this runtime/i)).toBeInTheDocument();
    expect(screen.queryByText("Dreaming")).toBeNull();
  });

  it("dials no RPC at all once AgentCore is confirmed", () => {
    render(<DreamsPanel />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
  });

  it("holds the RPC while the host is unknown, rather than guessing ECS", () => {
    gate = "loading";
    render(<DreamsPanel />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
    expect(screen.queryByText(/isn't available on this runtime/i)).toBeNull();
    expect(screen.queryByText("Dreaming")).toBeNull();
  });

  it("still fires the real RPC on ECS", () => {
    gate = "shown";
    render(<DreamsPanel />);
    expect(rpcMethods).toContain("doctor.memory.status");
  });
});
