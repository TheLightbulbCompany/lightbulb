// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openChatPanel = vi.fn();
const requestFocusComposer = vi.fn();
const openMarketplace = vi.fn();
let defaultId: string | undefined = "agent_1";
let agents = [{ id: "agent_1", name: "Nova" }];
let isSettled = true;
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ defaultId, agents, isSettled }),
}));
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
  defaultId = "agent_1";
  agents = [{ id: "agent_1", name: "Nova" }];
  isSettled = true;
  window.history.pushState(null, "", "/chat?panel=teams-dashboard");
});

describe("HomeQuickActions — every verb resolves to something real", () => {
  it("names the current default CEO and focuses its composer without touching the URL", () => {
    const spy = vi.fn();
    window.addEventListener("getstarted:focus-composer", spy);
    render(<HomeQuickActions stripeBound />);
    fireEvent.click(screen.getByRole("link", { name: "Ask Nova" }));
    expect(requestFocusComposer).toHaveBeenCalledWith("agent_1");
    expect(openChatPanel).toHaveBeenCalledWith("agent_1");
    expect(spy).toHaveBeenCalled();
    expect(window.location.search).toBe("?panel=teams-dashboard");
  });

  it("falls back to Ask CEO and does nothing on a plain click before the roster resolves", () => {
    // Without the guard, href="/chat" hard-navigates: a full SPA reload of the
    // page the owner is already on, dock layout and all. Doing nothing for the
    // moment before `defaultId` lands is the smaller failure.
    defaultId = undefined;
    agents = [];
    isSettled = false;
    render(<HomeQuickActions stripeBound />);
    const link = screen.getByRole("link", { name: "Ask CEO" });
    const ev = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    fireEvent(link, ev);
    expect(ev.defaultPrevented).toBe(true);
    expect(openChatPanel).not.toHaveBeenCalled();
    expect(requestFocusComposer).not.toHaveBeenCalled();
  });

  it("hides the CEO action when the settled roster has no resolvable CEO", () => {
    defaultId = undefined;
    agents = [];
    render(<HomeQuickActions stripeBound />);
    expect(screen.queryByRole("link", { name: "Ask CEO" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /New task/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Hire an agent/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Add a product/ })).toBeInTheDocument();
  });

  it("still lets a modifier click through when the roster has not resolved", () => {
    // A meta-click is the browser's new-tab gesture; preventing it would break
    // the one thing the real href is there for.
    defaultId = undefined;
    isSettled = false;
    render(<HomeQuickActions stripeBound />);
    const link = screen.getByRole("link", { name: "Ask CEO" });
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

  it("New task deep-links the Tasks New-task dialog", () => {
    render(<HomeQuickActions stripeBound />);
    expect(screen.getByRole("link", { name: /New task/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-issues&new=task",
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
