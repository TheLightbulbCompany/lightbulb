/**
 * M6 task 10-D, row 25. Channel wiring is M5/LATER on Hermes — the Channels
 * tab is dropped outright on a confirmed AgentCore workspace, same as the
 * existing unsubscribed-owner hide.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentInspectorPanel } from "../AgentInspectorPanel";

vi.mock("../AgentOverviewTab", () => ({
  AgentOverviewTab: ({ agentId }: { agentId: string }) => <div data-testid="overview-tab">{agentId}</div>,
}));
vi.mock("../AgentToolsTab", () => ({ AgentToolsTab: () => <div /> }));
vi.mock("../AgentChannelsSection", () => ({
  AgentChannelsSection: ({ agentId }: { agentId: string }) => <div data-testid="channels-tab">{agentId}</div>,
}));
vi.mock("../AgentInstructionsTab", () => ({ AgentInstructionsTab: () => <div /> }));
vi.mock("@/components/teams/panels/RoutinesPanel", () => ({ RoutinesPanel: () => <div /> }));
vi.mock("@/components/teams/agents/AgentOrgControls", () => ({ AgentOrgControls: () => <div /> }));
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "agent_42", name: "Scout" }], defaultId: "agent_42" }),
}));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({ data: undefined, error: undefined, isLoading: false, mutate: vi.fn() }),
  useRuntimeRpcMutation: () => vi.fn(),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: () => ({ data: undefined, error: undefined, isLoading: false, mutate: vi.fn() }) }),
}));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ isSubscribed: true }) }));

let gate: "loading" | "hidden" | "shown";
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});

describe("AgentInspectorPanel — Channels tab on AgentCore", () => {
  beforeEach(() => {
    gate = "hidden";
  });

  it("hides the Channels tab once AgentCore is confirmed, even for a subscribed owner", () => {
    render(<AgentInspectorPanel agentId="agent_42" />);
    expect(screen.queryByRole("tab", { name: "Channels" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
  });

  it("falls back to Overview when a ?inspectorTab=channels deep link arrives on AgentCore", () => {
    render(<AgentInspectorPanel agentId="agent_42" initialTab="channels" />);
    expect(screen.getByTestId("overview-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("channels-tab")).toBeNull();
  });

  it("still shows the Channels tab while the host isn't known yet", () => {
    gate = "loading";
    render(<AgentInspectorPanel agentId="agent_42" />);
    expect(screen.getByRole("tab", { name: "Channels" })).toBeInTheDocument();
  });

  it("still shows the Channels tab on ECS", () => {
    gate = "shown";
    render(<AgentInspectorPanel agentId="agent_42" />);
    expect(screen.getByRole("tab", { name: "Channels" })).toBeInTheDocument();
  });
});
