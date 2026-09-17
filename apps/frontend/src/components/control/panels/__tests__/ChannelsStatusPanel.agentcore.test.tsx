/**
 * M6 task 10-D, row 24. Channel wiring is M5/LATER on Hermes — the bridge
 * honestly answers `{channels: [], probe}`, which would otherwise always read
 * as "No channels configured" instead of "not available yet".
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { ChannelsStatusPanel } from "../ChannelsStatusPanel";

let gate: "loading" | "hidden" | "shown";
const rpcMethods: (string | null)[] = [];

vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    rpcMethods.push(method);
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
}));

describe("ChannelsStatusPanel on an AgentCore workspace", () => {
  beforeEach(() => {
    rpcMethods.length = 0;
    gate = "hidden";
  });

  it("renders a short notice instead of the probe UI", () => {
    render(<ChannelsStatusPanel />);
    expect(screen.getByText(/isn't available on this runtime/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /probe now/i })).toBeNull();
  });

  it("dials no RPC at all once AgentCore is confirmed", () => {
    render(<ChannelsStatusPanel />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
  });

  it("holds the RPC while the host is unknown, rather than guessing ECS", () => {
    gate = "loading";
    render(<ChannelsStatusPanel />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
    // Same loading chrome the panel already shows while data is in flight —
    // only the RPC itself is held, not the header/Probe affordance.
    expect(screen.getByText(/probing channels/i)).toBeInTheDocument();
  });

  it("still probes on ECS", () => {
    gate = "shown";
    render(<ChannelsStatusPanel />);
    expect(rpcMethods).toContain("channels.status");
  });
});
