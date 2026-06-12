import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { IntegrationsPanel } from "../IntegrationsPanel";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [] }) }));
vi.mock("@/components/settings/ConnectionsList", () => ({ ConnectionsList: () => <div data-testid="connections-list" /> }));
vi.mock("@/components/control/panels/GoogleAccountsSection", () => ({ GoogleAccountsSection: () => null }));
vi.mock("@/components/control/panels/McpServersTab", () => ({ McpServersTab: () => <div data-testid="mcp-servers-tab" /> }));
vi.mock("@/components/control/panels/PluginsTab", () => ({
  PluginsTab: () => <div data-testid="plugins-tab" />,
}));

describe("IntegrationsPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the connections list", () => {
    render(<IntegrationsPanel />);
    expect(screen.getByTestId("connections-list")).toBeInTheDocument();
  });

  it("shows raw MCP servers as a visible section, not behind Advanced", () => {
    render(<IntegrationsPanel />);
    expect(screen.getByText(/add your own mcp servers/i)).toBeInTheDocument();
    expect(screen.getByTestId("mcp-servers-tab")).toBeInTheDocument();
  });

  it("tucks plugin switches behind the Advanced disclosure", () => {
    render(<IntegrationsPanel />);
    expect(screen.queryByTestId("plugins-tab")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /advanced — plugins/i }));
    expect(screen.getByTestId("plugins-tab")).toBeInTheDocument();
  });
});
