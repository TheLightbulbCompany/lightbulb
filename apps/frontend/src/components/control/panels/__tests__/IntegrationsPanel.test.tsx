import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { IntegrationsPanel } from "../IntegrationsPanel";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [] }) }));
vi.mock("@/components/settings/ConnectionsList", () => ({ ConnectionsList: () => <div data-testid="connections-list" /> }));
vi.mock("@/components/control/panels/GoogleAccountsSection", () => ({ GoogleAccountsSection: () => null }));
vi.mock("@/components/control/panels/McpServersTab", () => ({ McpServersTab: () => <div data-testid="mcp-servers-tab" /> }));

describe("IntegrationsPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders the connections list and a collapsed raw-MCP advanced section", () => {
    render(<IntegrationsPanel />);
    expect(screen.getByTestId("connections-list")).toBeInTheDocument();
    expect(screen.getByText("Advanced — raw MCP servers")).toBeInTheDocument();
    // Collapsed by default — McpServersTab not mounted.
    expect(screen.queryByTestId("mcp-servers-tab")).toBeNull();
  });

  it("expands the raw-MCP section on click", () => {
    render(<IntegrationsPanel />);
    fireEvent.click(screen.getByText("Advanced — raw MCP servers"));
    expect(screen.getByTestId("mcp-servers-tab")).toBeInTheDocument();
  });
});
