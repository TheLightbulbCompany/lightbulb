import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ControlPanelRouter } from "../ControlPanelRouter";

let gate: "loading" | "hidden" | "shown" = "loading";
vi.mock("@lightbulb/shared", () => ({ useHiddenOnAgentCore: () => gate }));

// Stub every panel the router imports so we can assert which one mounts
// without dragging their full deps into the test sandbox.
vi.mock("../panels/LLMPanel", () => ({ LLMPanel: () => <div data-testid="llm-panel" /> }));
vi.mock("../panels/OverviewPanel", () => ({
  OverviewPanel: () => <div data-testid="overview-panel" />,
}));
vi.mock("../panels/SessionsPanel", () => ({
  SessionsPanel: () => <div data-testid="sessions-panel" />,
}));
vi.mock("../panels/UsagePanel", () => ({ UsagePanel: () => <div /> }));
vi.mock("../panels/AgentsPanel", () => ({ AgentsPanel: () => <div /> }));
vi.mock("../panels/SkillsTab", () => ({ SkillsTab: () => <div data-testid="skills-store" /> }));
vi.mock("../panels/IntegrationsPanel", () => ({ IntegrationsPanel: () => <div /> }));
vi.mock("../panels/ChannelsStatusPanel", () => ({ ChannelsStatusPanel: () => <div /> }));
vi.mock("../panels/SkillProposalsPanel", () => ({ SkillProposalsPanel: () => <div>SkillProposalsPanel</div> }));
vi.mock("../panels/DreamsPanel", () => ({ DreamsPanel: () => <div data-testid="dreams-panel" /> }));
vi.mock("../panels/AgentInspectorPanel", () => ({
  AgentInspectorPanel: () => <div data-testid="agent-inspector-panel" />,
}));

// Legacy-key resolution (credits→llm) moved OUT of this router into the shared
// panelRegistry (resolvePanelAlias) — the shell resolves the alias before
// mounting the router, replace-only, so the old push-trap is gone. That
// mapping is covered by panelRegistry.test.ts; this router now only routes
// already-resolved keys.
describe("ControlPanelRouter routing", () => {
  // Panels load via next/dynamic, so they resolve asynchronously —
  // findByTestId retries until the lazy boundary settles.
  it("renders mapped panels directly for their key", async () => {
    render(<ControlPanelRouter panel="llm" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("llm-panel")).toBeInTheDocument();
  });

  it("falls back to OverviewPanel for unknown panel keys", async () => {
    render(<ControlPanelRouter panel="does-not-exist" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("overview-panel")).toBeInTheDocument();
  });

  it("routes 'dreams' to the standalone Dreams panel again", async () => {
    // It spent a while folded into Brain as a tab; ?panel=dreams must open the
    // control panel, not fall through to Overview.
    render(<ControlPanelRouter panel="dreams" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("dreams-panel")).toBeInTheDocument();
  });

  it("does NOT call onPanelChange for a plain key", async () => {
    const onPanelChange = vi.fn();
    render(<ControlPanelRouter panel="overview" onPanelChange={onPanelChange} />);
    await screen.findByTestId("overview-panel");
    expect(onPanelChange).not.toHaveBeenCalled();
  });

  it("mounts the skills store directly (no Capabilities tab wrapper)", async () => {
    render(<ControlPanelRouter panel="skills" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("skills-store")).toBeInTheDocument();
  });

  it("routes the agent-inspector key to AgentInspectorPanel", async () => {
    render(<ControlPanelRouter panel="agent-inspector" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("agent-inspector-panel")).toBeInTheDocument();
  });

  it("shows the loading fallback before a panel chunk resolves", () => {
    const { container } = render(
      <ControlPanelRouter panel="sessions" onPanelChange={vi.fn()} />,
    );
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  });
});

// Ruling M6-R54. Sessions has no AgentCore shape: the backend's session guard is a PRIVACY
// rule (a person sees only their own personal sessions) and every Hermes session here is
// room- or event-scoped, so the panel could only ever show an empty table. It is hidden
// rather than widened.
describe("ControlPanelRouter on an AgentCore workspace", () => {
  it("falls the sessions key back to Overview", async () => {
    gate = "hidden";
    render(<ControlPanelRouter panel="sessions" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("overview-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("sessions-panel")).toBeNull();
  });

  it("still mounts the panel on ECS", async () => {
    gate = "shown";
    render(<ControlPanelRouter panel="sessions" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("sessions-panel")).toBeInTheDocument();
  });

  it("mounts it while the runtime is UNKNOWN — hiding on 'loading' would blank it for ECS", async () => {
    gate = "loading";
    render(<ControlPanelRouter panel="sessions" onPanelChange={vi.fn()} />);
    expect(await screen.findByTestId("sessions-panel")).toBeInTheDocument();
  });
});
