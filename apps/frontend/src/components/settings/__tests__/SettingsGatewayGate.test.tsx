import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// The gate wraps /settings in the GatewayProvider so the gateway-bound config
// panels (Skills, Skill Proposals, Dreams, Channel Status, MCP, LLM health dot)
// can mount.

// Stub GatewayProvider so the test never opens a real WebSocket; we only assert
// the provider boundary wraps the children.
vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="gateway-provider">{children}</div>
  ),
}));

import { SettingsGatewayGate } from "../SettingsGatewayGate";

describe("SettingsGatewayGate", () => {
  it("wraps children INSIDE the GatewayProvider", () => {
    render(
      <SettingsGatewayGate>
        <span data-testid="settings-child">child content</span>
      </SettingsGatewayGate>,
    );
    const provider = screen.getByTestId("gateway-provider");
    const child = screen.getByTestId("settings-child");
    expect(provider).toBeInTheDocument();
    // Nested inside, not a sibling — this is what makes the WS available to
    // every gateway-bound /settings panel.
    expect(provider).toContainElement(child);
  });
});
