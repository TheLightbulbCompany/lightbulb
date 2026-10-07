import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// The server-component layout delegates the shared live/runtime provider wrap
// to the client-side SettingsGatewayGate. This test asserts that delegation.
vi.mock("@/hooks/useLiveEvents", () => ({
  LiveEventsProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="live-events-provider">{children}</div>
  ),
}));

import SettingsLayout from "../layout";

describe("SettingsLayout (LiveEventsProvider wrap)", () => {
  it("wraps children inside the LiveEventsProvider", () => {
    render(
      <SettingsLayout>
        <span data-testid="settings-child">child content</span>
      </SettingsLayout>,
    );

    const provider = screen.getByTestId("live-events-provider");
    const child = screen.getByTestId("settings-child");
    expect(provider).toBeInTheDocument();
    // The child must be nested inside the provider, not a sibling.
    expect(provider).toContainElement(child);
  });
});
