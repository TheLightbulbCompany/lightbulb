import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { CapabilitiesPanel } from "../CapabilitiesPanel";

vi.mock("../PluginsTab", () => ({ PluginsTab: () => <div data-testid="plugins-tab" /> }));
vi.mock("../SkillsTab", () => ({ SkillsTab: () => <div data-testid="skills-tab" /> }));

describe("CapabilitiesPanel", () => {
  it("renders Plugins and Skills tabs, defaulting to Plugins", () => {
    render(<CapabilitiesPanel />);
    expect(screen.getByRole("button", { name: /plugins/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /skills/i })).toBeInTheDocument();
    expect(screen.getByTestId("plugins-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("skills-tab")).toBeNull();
  });

  it("switches to the Skills tab on click", () => {
    render(<CapabilitiesPanel />);
    fireEvent.click(screen.getByRole("button", { name: /skills/i }));
    expect(screen.getByTestId("skills-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("plugins-tab")).toBeNull();
  });
});
