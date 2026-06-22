import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// Six Settings config panels are gateway-bound (Skills, Skill Proposals,
// Dreams, Channel Status, MCP/IntegrationsPanel, the LLM health dot). They can
// only mount once /settings/* is wrapped in the same GatewayProvider the chat
// surface uses — but ONLY under the unified-nav flag. Flag-OFF, /settings stays
// byte-identical to today's app: pure-REST, NO idle WS, so NO provider.
//
// The layout delegates the conditional wrap to SettingsGatewayGate (a client
// component, because the flag is a client hook). This test asserts that
// delegation: flag-OFF → no provider; flag-ON → children inside the provider.
let unified = false;
vi.mock("@/hooks/useUnifiedNav", () => ({
  useUnifiedNav: (): boolean => unified,
}));

// Stub GatewayProvider so we don't open a real WebSocket; we only verify
// presence/absence of the provider boundary around the layout's children.
vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="gateway-provider">{children}</div>
  ),
}));

import SettingsLayout from "../layout";

describe("SettingsLayout (flag-gated GatewayProvider)", () => {
  beforeEach(() => {
    unified = false;
  });

  it("flag-OFF: renders children WITHOUT a GatewayProvider (byte-identical, no WS)", () => {
    unified = false;
    render(
      <SettingsLayout>
        <span data-testid="settings-child">child content</span>
      </SettingsLayout>,
    );
    expect(screen.getByTestId("settings-child")).toBeInTheDocument();
    expect(screen.queryByTestId("gateway-provider")).not.toBeInTheDocument();
  });

  it("flag-ON: wraps children inside the GatewayProvider", () => {
    unified = true;
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
