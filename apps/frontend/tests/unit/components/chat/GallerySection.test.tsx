import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GallerySection } from "@/components/chat/GallerySection";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

// Test-only probe: surface the live drawer-context value so assertions can
// inspect it. Mutating a module-level binding from inside a component would
// trip the react-hooks/globals rule; mutating a property of a stable holder
// object trips react-hooks/immutability. Both rules are OK here — Probe
// renders as a leaf, never re-renders against this value, and the test only
// reads after the hook fires. Disable the immutability rule narrowly.
const capturedRef: { current: ReturnType<typeof useMarketplaceDrawer> | null } = {
  current: null,
};
function Probe() {
  // eslint-disable-next-line react-hooks/immutability
  capturedRef.current = useMarketplaceDrawer();
  return null;
}

describe("GallerySection (2-button marketplace block)", () => {
  it("renders Browse and Publish buttons under a 'Marketplace' label", () => {
    render(
      <MarketplaceDrawerProvider>
        <GallerySection />
      </MarketplaceDrawerProvider>,
    );
    expect(screen.getByText(/marketplace/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /^browse$/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /publish/i })).toBeVisible();
  });

  it("Browse opens drawer in browse view", async () => {
    const user = userEvent.setup();
    render(
      <MarketplaceDrawerProvider>
        <GallerySection />
        <Probe />
      </MarketplaceDrawerProvider>,
    );
    await user.click(screen.getByRole("button", { name: /^browse$/i }));
    expect(capturedRef.current?.state.isOpen).toBe(true);
    expect(capturedRef.current?.state.view).toBe("browse");
  });

  it("Publish opens drawer in publish view", async () => {
    const user = userEvent.setup();
    render(
      <MarketplaceDrawerProvider>
        <GallerySection />
        <Probe />
      </MarketplaceDrawerProvider>,
    );
    await user.click(screen.getByRole("button", { name: /publish/i }));
    expect(capturedRef.current?.state.isOpen).toBe(true);
    expect(capturedRef.current?.state.view).toBe("publish");
  });
});
