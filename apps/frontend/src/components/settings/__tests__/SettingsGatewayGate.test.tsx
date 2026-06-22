import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// The gate flips the GatewayProvider wrap on the unified-nav flag. Flag-OFF the
// Settings surface is byte-identical to today's app — pure REST, NO idle WS — so
// the provider must NOT mount. Flag-ON the gateway-bound [gw] panels need it, so
// it must wrap.
let unified = false;
vi.mock("@/hooks/useUnifiedNav", () => ({
  useUnifiedNav: (): boolean => unified,
}));

// Stub GatewayProvider so the gate test never opens a real WebSocket; we only
// assert presence/absence of the provider boundary around the children.
vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="gateway-provider">{children}</div>
  ),
}));

import { SettingsGatewayGate } from "../SettingsGatewayGate";

describe("SettingsGatewayGate (flag-gated provider wrap)", () => {
  beforeEach(() => {
    unified = false;
  });

  it("flag-OFF: renders children WITHOUT a GatewayProvider (no idle WS)", () => {
    unified = false;
    render(
      <SettingsGatewayGate>
        <span data-testid="settings-child">child content</span>
      </SettingsGatewayGate>,
    );
    expect(screen.getByTestId("settings-child")).toBeInTheDocument();
    expect(screen.queryByTestId("gateway-provider")).not.toBeInTheDocument();
  });

  it("flag-ON: wraps children INSIDE the GatewayProvider", () => {
    unified = true;
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
