// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openChatPanel = vi.fn();
const requestFocusComposer = vi.fn();
const openMarketplace = vi.fn();
let desktop = true;
let defaultId: string | undefined = "agent_1";
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ defaultId, agents: [], isSettled: true }),
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
  defaultId = "agent_1";
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

  it("does nothing on a plain Ask Friday click before the roster resolves", () => {
    // Without the guard, href="/chat" hard-navigates: a full SPA reload of the
    // page the owner is already on, dock layout and all. Doing nothing for the
    // moment before `defaultId` lands is the smaller failure.
    defaultId = undefined;
    render(<HomeQuickActions stripeBound />);
    const link = screen.getByRole("link", { name: /Ask Friday/ });
    const ev = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    fireEvent(link, ev);
    expect(ev.defaultPrevented).toBe(true);
    expect(openChatPanel).not.toHaveBeenCalled();
    expect(requestFocusComposer).not.toHaveBeenCalled();
  });

  it("still lets a modifier click through when the roster has not resolved", () => {
    // A meta-click is the browser's new-tab gesture; preventing it would break
    // the one thing the real href is there for.
    defaultId = undefined;
    render(<HomeQuickActions stripeBound />);
    const link = screen.getByRole("link", { name: /Ask Friday/ });
    const ev = new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      button: 0,
    });
    Object.defineProperty(ev, "metaKey", { value: true });
    // Read the flag at document level — React 19 delegates from the container
    // root, so this runs after the component's handler — then cancel, or jsdom
    // tries to follow href="/chat" and prints a "navigation not implemented"
    // stack that looks like a failure.
    let prevented: boolean | undefined;
    document.addEventListener(
      "click",
      (e) => {
        prevented = e.defaultPrevented;
        e.preventDefault();
      },
      { once: true },
    );
    fireEvent(link, ev);
    expect(prevented).toBe(false);
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
