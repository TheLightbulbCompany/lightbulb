/**
 * Hermes gates by TOOLSET, not per tool, so there is no allow/deny list to edit —
 * and the editor was worse than empty: Save called `agents.update {tools}`, which
 * answered `{"ok": true}` and wrote nothing. The backend now refuses that call;
 * this is the surface that stops asking for it.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { AgentToolsTab } from "../AgentToolsTab";

const useRuntimeRpc = vi.fn(() => ({ data: undefined, error: undefined, isLoading: false, mutate: vi.fn() }));
const mockCallRpc = vi.fn();
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (...a: unknown[]) => useRuntimeRpc(...(a as [])),
  useRuntimeRpcMutation: () => mockCallRpc,
}));

describe("AgentToolsTab", () => {
  it("renders a short notice instead of the editor", () => {
    render(<AgentToolsTab agentId="a1" />);
    expect(screen.getByText(/isn't available on this runtime/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /enable all/i })).toBeNull();
  });

  it("dials no RPC at all — a stubbed tools.catalog would start a microVM to render an error", () => {
    render(<AgentToolsTab agentId="a1" />);
    expect(useRuntimeRpc).not.toHaveBeenCalled();
    expect(mockCallRpc).not.toHaveBeenCalled();
  });
});
