import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ControlPanelRouter } from "../ControlPanelRouter";

// Stub every panel the router imports so we can assert which one mounts
// without dragging their full deps into the test sandbox.
vi.mock("../panels/AgentsPanel", () => ({ AgentsPanel: () => <div data-testid="agents-panel" /> }));
vi.mock("../panels/AgentInspectorPanel", () => ({
  AgentInspectorPanel: () => <div data-testid="agent-inspector-panel" />,
}));

// The control dock keeps only the agent detail views. Overview, Usage, Skills,
// Integrations, Connections and Model moved to the one Settings page; their
// URLs redirect there (panelRegistry.settingsHrefFor) before reaching a router.
describe("ControlPanelRouter routing", () => {
  // Panels load via next/dynamic, so they resolve asynchronously —
  // findByTestId retries until the lazy boundary settles.
  // First: once a chunk has loaded, later renders skip the fallback.
  it("shows the loading fallback before a panel chunk resolves", () => {
    const { container } = render(<ControlPanelRouter panel="agents" />);
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  });

  it("routes the agents key to AgentsPanel", async () => {
    render(<ControlPanelRouter panel="agents" />);
    expect(await screen.findByTestId("agents-panel")).toBeInTheDocument();
  });

  it("routes the agent-inspector key to AgentInspectorPanel", async () => {
    render(<ControlPanelRouter panel="agent-inspector" />);
    expect(await screen.findByTestId("agent-inspector-panel")).toBeInTheDocument();
  });

  it.each(["overview", "usage", "skills", "integrations", "connections", "llm", "dreams", "does-not-exist"])(
    "renders nothing for %s",
    (panel) => {
      const { container } = render(<ControlPanelRouter panel={panel} />);
      expect(container).toBeEmptyDOMElement();
    },
  );
});
