import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ControlPanelRouter } from "../ControlPanelRouter";

// Stub every panel the router imports so we can assert which one mounts
// without dragging their full deps into the test sandbox.
vi.mock("../panels/LLMPanel", () => ({ LLMPanel: () => <div data-testid="llm-panel" /> }));
vi.mock("../panels/OverviewPanel", () => ({
  OverviewPanel: () => <div data-testid="overview-panel" />,
}));
vi.mock("../panels/UsagePanel", () => ({ UsagePanel: () => <div data-testid="usage-panel" /> }));
vi.mock("../panels/AgentsPanel", () => ({ AgentsPanel: () => <div /> }));
vi.mock("../panels/SkillsTab", () => ({ SkillsTab: () => <div data-testid="skills-store" /> }));
vi.mock("../panels/IntegrationsPanel", () => ({ IntegrationsPanel: () => <div /> }));
vi.mock("@/components/mail/MailPanel", () => ({
  MailPanel: ({ item }: { item: string | null }) => <div data-testid="mail-panel">{item}</div>,
}));
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

  // Dreams, Sessions and Channel Status were OpenClaw-shaped surfaces with no
  // equivalent on this runtime. Their keys are gone rather than gated, so a
  // stale deep link lands on Overview like any other unknown key.
  it.each(["dreams", "sessions", "channels"])(
    "falls a retired %s deep link back to Overview",
    async (panel) => {
      render(<ControlPanelRouter panel={panel} onPanelChange={vi.fn()} />);
      expect(await screen.findByTestId("overview-panel")).toBeInTheDocument();
    },
  );

  it("renders Mail for the mobile shell, with the open item", async () => {
    render(<ControlPanelRouter panel="mail" panelParams={{ item: "mail:k1" }} />);
    expect(await screen.findByTestId("mail-panel")).toHaveTextContent("mail:k1");
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
      <ControlPanelRouter panel="usage" onPanelChange={vi.fn()} />,
    );
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
  });
});
