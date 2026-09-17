/**
 * M6 task 10, row 12. Hermes gates by TOOLSET, not per tool, so there is no allow/deny list to
 * edit on a migrated workspace — and the editor was worse than empty: Save called
 * `agents.update {tools}`, which answered `{"ok": true}` and wrote nothing. The backend now
 * refuses that call; this is the surface that stops asking for it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { AgentToolsTab } from "../AgentToolsTab";

let runtimeHost: "ecs" | "agentcore" | undefined;
const rpcMethods: (string | null)[] = [];
const mockCallRpc = vi.fn();

vi.mock("@lightbulb/shared", () => ({ useRuntimeHost: () => runtimeHost }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    rpcMethods.push(method);
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useRuntimeRpcMutation: () => mockCallRpc,
}));

describe("AgentToolsTab on an AgentCore workspace", () => {
  beforeEach(() => {
    rpcMethods.length = 0;
    mockCallRpc.mockReset();
    runtimeHost = "agentcore";
  });

  it("renders a short notice instead of the editor", () => {
    render(<AgentToolsTab agentId="a1" />);
    expect(screen.getByText(/isn't available on this runtime/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /enable all/i })).toBeNull();
  });

  it("dials no RPC at all — a stubbed tools.catalog would start a microVM to render an error", () => {
    render(<AgentToolsTab agentId="a1" />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
    expect(mockCallRpc).not.toHaveBeenCalled();
  });

  it("still renders the editor on ECS", () => {
    runtimeHost = "ecs";
    render(<AgentToolsTab agentId="a1" />);
    expect(screen.getByText("Tool Access")).toBeInTheDocument();
    expect(rpcMethods).toContain("tools.catalog");
  });

  it("holds both RPCs while the host is unknown, rather than guessing ECS", () => {
    // Review fix round 1 (I1). `undefined` is "not known for THIS workspace" — no status has
    // landed, or one was just switched into. Guessing ECS and firing is the mistake with a
    // ~55s microVM cold start attached, and it is the same policy gap as the store leak (C1).
    runtimeHost = undefined;
    render(<AgentToolsTab agentId="a1" />);
    expect(rpcMethods.every((method) => method === null)).toBe(true);
    expect(screen.queryByText("Tool Access")).toBeNull();
    expect(screen.queryByRole("button", { name: /save/i })).toBeNull();
  });

  it("draws the editor as soon as the host is known to be ECS", () => {
    runtimeHost = undefined;
    const { rerender } = render(<AgentToolsTab agentId="a1" />);
    expect(screen.queryByText("Tool Access")).toBeNull();
    runtimeHost = "ecs";
    rerender(<AgentToolsTab agentId="a1" />);
    expect(screen.getByText("Tool Access")).toBeInTheDocument();
    expect(rpcMethods).toContain("tools.catalog");
  });
});
