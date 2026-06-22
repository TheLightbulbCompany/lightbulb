import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Six Settings panels are gateway-bound (Skills, Skill Proposals, Dreams,
// Channel Status, MCP/IntegrationsPanel, the LLM health dot). They can only
// mount once /settings/* is wrapped in the same GatewayProvider the chat
// surface uses. This test asserts the wrap exists — the enabler increment.
//
// Stub GatewayProvider so we don't open a real WebSocket; we only verify that
// the layout renders its children *inside* the provider boundary.
vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="gateway-provider">{children}</div>
  ),
}));

import SettingsLayout from "../layout";

describe("SettingsLayout", () => {
  it("wraps children in the GatewayProvider", () => {
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
