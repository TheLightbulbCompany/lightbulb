import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Five Settings config panels are gateway-bound (Skills, Skill Proposals,
// Channel Status, MCP/IntegrationsPanel, the LLM health dot). They mount
// once /settings/* is wrapped in the same GatewayProvider the chat surface uses.
// The server-component layout delegates that wrap to SettingsGatewayGate (a
// client component). This test asserts the delegation.

// Stub GatewayProvider so we don't open a real WebSocket; we only verify the
// provider boundary wraps the layout's children.
vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="gateway-provider">{children}</div>
  ),
}));

import SettingsLayout from "../layout";

describe("SettingsLayout (GatewayProvider wrap)", () => {
  it("wraps children inside the GatewayProvider", () => {
    render(
      <SettingsLayout>
        <span data-testid="settings-child">child content</span>
      </SettingsLayout>,
    );

    const provider = screen.getByTestId("gateway-provider");
    const child = screen.getByTestId("settings-child");
    expect(provider).toBeInTheDocument();
    // The child must be nested *inside* the provider, not a sibling — that is
    // what makes the gateway WS available to every /settings panel.
    expect(provider).toContainElement(child);
  });
});
