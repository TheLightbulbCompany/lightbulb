import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Settings routes need the shared live/runtime provider so authenticated
// configuration panels can issue canonical HTTP RPCs and receive invalidations.
vi.mock("@/hooks/useLiveEvents", () => ({
  LiveEventsProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="live-events-provider">{children}</div>
  ),
}));

import { SettingsGatewayGate } from "../SettingsGatewayGate";

describe("SettingsGatewayGate", () => {
  it("wraps children inside the LiveEventsProvider", () => {
    render(
      <SettingsGatewayGate>
        <span data-testid="settings-child">child content</span>
      </SettingsGatewayGate>,
    );
    const provider = screen.getByTestId("live-events-provider");
    const child = screen.getByTestId("settings-child");
    expect(provider).toBeInTheDocument();
    // Nested inside, not a sibling, so every settings panel shares one client.
    expect(provider).toContainElement(child);
  });
});
