import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { IntegrationsPanel } from "../IntegrationsPanel";

vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));
vi.mock("@/components/control/panels/McpServersTab", () => ({ McpServersTab: () => <div data-testid="mcp-servers-tab" /> }));

// Settings → Advanced → MCP servers. The provider catalog it used to repeat
// lives once, in Settings → Connections.
describe("IntegrationsPanel (MCP servers)", () => {
  it("shows raw MCP servers as a visible section, not behind Advanced", () => {
    const { container } = render(<IntegrationsPanel />);
    expect(container.querySelector("#panel-mcp")).toBeInTheDocument();
    expect(screen.getByText(/add your own mcp servers/i)).toBeInTheDocument();
    expect(screen.getByTestId("mcp-servers-tab")).toBeInTheDocument();
  });

  // `config.plugins.entries` has no reader on this runtime, so the Advanced
  // disclosure is gone rather than opening onto a permanently-empty panel.
  it("offers no Advanced plugins disclosure", () => {
    render(<IntegrationsPanel />);
    expect(screen.queryByRole("button", { name: /advanced/i })).toBeNull();
  });
});
