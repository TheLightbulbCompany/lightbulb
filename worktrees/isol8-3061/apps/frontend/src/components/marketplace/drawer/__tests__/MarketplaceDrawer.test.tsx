import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
import {
  MarketplaceDrawerProvider,
  useMarketplaceDrawer,
} from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

vi.mock("@/components/marketplace/drawer/BrowseView", () => ({
  BrowseView: () => <div data-testid="browse-view">browse</div>,
}));
vi.mock("@/components/marketplace/drawer/MyListingsView", () => ({
  MyListingsView: () => <div data-testid="my-listings-view">my listings</div>,
}));
vi.mock("@/components/marketplace/drawer/PublishWizard", () => ({
  PublishWizard: () => <div data-testid="publish-wizard">publish</div>,
}));

function Wrapper({ children }: { children: React.ReactNode }) {
  return <MarketplaceDrawerProvider>{children}</MarketplaceDrawerProvider>;
}

function OpenButton({ view }: { view: "browse" | "mine" | "publish" }) {
  const { open } = useMarketplaceDrawer();
  return <button onClick={() => open({ view })}>open-{view}</button>;
}

describe("MarketplaceDrawer", () => {
  it("renders nothing when closed", () => {
    render(<MarketplaceDrawer />, { wrapper: Wrapper });
    expect(screen.queryByTestId("browse-view")).not.toBeInTheDocument();
  });

  it("opens with browse view by default and shows tabs + publish button + full-page link", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="browse" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-browse"));
    expect(screen.getByTestId("browse-view")).toBeVisible();
    expect(screen.getByRole("button", { name: /^browse$/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /^my listings$/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /publish your agent/i })).toBeVisible();
    expect(screen.getByRole("link", { name: /open in full page/i })).toHaveAttribute(
      "href",
      "/marketplace",
    );
  });

  it("clicking a tab switches the view in place", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="browse" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-browse"));
    await user.click(screen.getByRole("button", { name: /^my listings$/i }));
    expect(screen.getByTestId("my-listings-view")).toBeVisible();
    expect(screen.queryByTestId("browse-view")).not.toBeInTheDocument();
  });

  it("publish mode swaps the header (back arrow + step label, no tabs)", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="publish" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-publish"));
    expect(screen.getByTestId("publish-wizard")).toBeVisible();
    expect(screen.getByRole("button", { name: /^marketplace$/i })).toBeVisible();
    expect(screen.queryByRole("button", { name: /^browse$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open in full page/i })).not.toBeInTheDocument();
  });

  it("clicking the close button closes the drawer", async () => {
    const user = userEvent.setup();
    render(
      <>
        <OpenButton view="browse" />
        <MarketplaceDrawer />
      </>,
      { wrapper: Wrapper },
    );
    await user.click(screen.getByText("open-browse"));
    await user.click(screen.getByRole("button", { name: /close drawer/i }));
    expect(screen.queryByTestId("browse-view")).not.toBeInTheDocument();
  });
});
