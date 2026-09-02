// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openChatPanel = vi.fn();
const requestFocusComposer = vi.fn();
const openMarketplace = vi.fn();
let desktop = true;
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ defaultId: "agent_1", agents: [], isSettled: true }),
}));
vi.mock("@/hooks/useIsDesktop", () => ({ useIsDesktop: () => desktop }));
vi.mock("@/components/workspace/workspaceController", () => ({
  openChatPanel: (id: string) => openChatPanel(id),
}));
vi.mock("@/lib/focusComposerRequest", () => ({
  requestFocusComposer: (id: string) => requestFocusComposer(id),
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: openMarketplace }),
}));

import { HomeQuickActions } from "../HomeQuickActions";

beforeEach(() => {
  vi.clearAllMocks();
  desktop = true;
  window.history.pushState(null, "", "/chat?panel=teams-dashboard");
});

describe("HomeQuickActions — every verb resolves to something real", () => {
  it("Ask Friday focuses the default agent's composer on desktop without touching the URL", () => {
    const spy = vi.fn();
    window.addEventListener("getstarted:focus-composer", spy);
    render(<HomeQuickActions stripeBound />);
    fireEvent.click(screen.getByRole("link", { name: /Ask Friday/ }));
    expect(requestFocusComposer).toHaveBeenCalledWith("agent_1");
    expect(openChatPanel).toHaveBeenCalledWith("agent_1");
    expect(spy).toHaveBeenCalled();
    expect(window.location.search).toBe("?panel=teams-dashboard");
  });

  it("Ask Friday swaps to the chat view on mobile, where the dock is not mounted", () => {
    desktop = false;
    render(<HomeQuickActions stripeBound />);
    fireEvent.click(screen.getByRole("link", { name: /Ask Friday/ }));
    expect(openChatPanel).not.toHaveBeenCalled();
    expect(window.location.pathname + window.location.search).toBe("/chat");
  });

  it("New task deep-links the Inbox New-task dialog", () => {
    render(<HomeQuickActions stripeBound />);
    expect(screen.getByRole("link", { name: /New task/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-inbox&newIssue=1",
    );
  });

  it("Hire an agent opens the marketplace drawer on browse", () => {
    render(<HomeQuickActions stripeBound />);
    fireEvent.click(screen.getByRole("button", { name: /Hire an agent/ }));
    expect(openMarketplace).toHaveBeenCalledWith({ view: "browse" });
  });

  it("Add a product goes to the owner's Stripe Dashboard, and only when Stripe is bound", () => {
    const { rerender } = render(<HomeQuickActions stripeBound />);
    expect(screen.getByRole("link", { name: /Add a product/ })).toHaveAttribute(
      "href",
      "https://dashboard.stripe.com/products/create",
    );
    rerender(<HomeQuickActions stripeBound={false} />);
    expect(screen.queryByRole("link", { name: /Add a product/ })).not.toBeInTheDocument();
  });
});
