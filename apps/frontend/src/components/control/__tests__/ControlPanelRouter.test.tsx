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
vi.mock("../panels/SessionsPanel", () => ({ SessionsPanel: () => <div /> }));
vi.mock("../panels/UsagePanel", () => ({ UsagePanel: () => <div /> }));
vi.mock("../panels/CronPanel", () => ({ CronPanel: () => <div /> }));
vi.mock("../panels/AgentsPanel", () => ({ AgentsPanel: () => <div /> }));
vi.mock("../panels/SkillsTab", () => ({ SkillsTab: () => <div data-testid="skills-store" /> }));
vi.mock("../panels/IntegrationsPanel", () => ({ IntegrationsPanel: () => <div /> }));
vi.mock("../panels/ChannelsStatusPanel", () => ({ ChannelsStatusPanel: () => <div /> }));

describe("ControlPanelRouter credits redirect", () => {
  it("renders Model & Credits when the legacy ?panel=credits key arrives", () => {
    const onPanelChange = vi.fn();
    render(<ControlPanelRouter panel="credits" onPanelChange={onPanelChange} />);
    expect(screen.getByTestId("llm-panel")).toBeInTheDocument();
    expect(onPanelChange).toHaveBeenCalledWith("llm");
  });

  it("renders mapped panels directly for non-legacy keys", () => {
    render(<ControlPanelRouter panel="llm" onPanelChange={vi.fn()} />);
    expect(screen.getByTestId("llm-panel")).toBeInTheDocument();
  });

  it("falls back to OverviewPanel for unknown panel keys", () => {
    render(<ControlPanelRouter panel="does-not-exist" onPanelChange={vi.fn()} />);
    expect(screen.getByTestId("overview-panel")).toBeInTheDocument();
  });

  it("does NOT call onPanelChange for non-legacy keys", () => {
    const onPanelChange = vi.fn();
    render(<ControlPanelRouter panel="overview" onPanelChange={onPanelChange} />);
    expect(onPanelChange).not.toHaveBeenCalled();
  });

  it("mounts the skills store directly (no Capabilities tab wrapper)", () => {
    render(<ControlPanelRouter panel="skills" onPanelChange={vi.fn()} />);
    expect(screen.getByTestId("skills-store")).toBeInTheDocument();
  });

  it("redirect path tolerates a missing onPanelChange callback", () => {
    render(<ControlPanelRouter panel="credits" />);
    expect(screen.getByTestId("llm-panel")).toBeInTheDocument();
  });
});
