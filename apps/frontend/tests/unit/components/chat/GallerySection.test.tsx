import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GallerySection } from "@/components/chat/GallerySection";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

let captured: ReturnType<typeof useMarketplaceDrawer> | null = null;
function Probe() {
  captured = useMarketplaceDrawer();
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
    expect(captured?.state.isOpen).toBe(true);
    expect(captured?.state.view).toBe("browse");
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
    expect(captured?.state.isOpen).toBe(true);
    expect(captured?.state.view).toBe("publish");
  });
});
