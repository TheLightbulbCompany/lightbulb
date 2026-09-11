// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openChatPanel = vi.fn();
const requestFocusComposer = vi.fn();
const openMarketplace = vi.fn();
let desktop = true;
let defaultId: string | undefined = "agent_1";
let agents = [{ id: "agent_1", name: "Nova" }];
let isSettled = true;
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ defaultId, agents, isSettled }),
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
  agents = [{ id: "agent_1", name: "Nova" }];
  isSettled = true;
  window.history.pushState(null, "", "/chat?panel=teams-dashboard");
});

describe("HomeQuickActions — every verb resolves to something real", () => {
  it("names the current default CEO and focuses its composer on desktop without touching the URL", () => {
    const spy = vi.fn();
    window.addEventListener("getstarted:focus-composer", spy);
    render(<HomeQuickActions stripeBound />);
    fireEvent.click(screen.getByRole("link", { name: "Ask Nova" }));
    expect(requestFocusComposer).toHaveBeenCalledWith("agent_1");
    expect(openChatPanel).toHaveBeenCalledWith("agent_1");
    expect(spy).toHaveBeenCalled();
    expect(window.location.search).toBe("?panel=teams-dashboard");
  });

  it("the CEO action swaps to the chat view on mobile, where the dock is not mounted", () => {
    desktop = false;
    const order: Array<{ type: string; agentId: string; source?: string }> = [];
    const onSelectAgent = (e: Event) => {
      const d = (e as CustomEvent).detail;
      order.push({ type: "selectAgent", agentId: d.agentId, source: d.source });
    };
    const onFocusComposer = (e: Event) =>
      order.push({ type: "getstarted:focus-composer", agentId: (e as CustomEvent).detail.agentId });
    window.addEventListener("selectAgent", onSelectAgent);
    window.addEventListener("getstarted:focus-composer", onFocusComposer);
    render(<HomeQuickActions stripeBound />);
    fireEvent.click(screen.getByRole("link", { name: "Ask Nova" }));
    expect(openChatPanel).not.toHaveBeenCalled();
    expect(window.location.pathname + window.location.search).toBe("/chat");
    // The mobile shell keeps its own selectedAgentId, which would otherwise
    // outrank defaultId and strand the focus event on the wrong agent — so
    // selectAgent must fire, and fire before the focus event.
    expect(order).toEqual([
      // `source: "notification"` is the contract ChatLayout's own listener
      // requires; without it the header's agent actions stay on the old agent.
      { type: "selectAgent", agentId: "agent_1", source: "notification" },
      { type: "getstarted:focus-composer", agentId: "agent_1" },
    ]);
    window.removeEventListener("selectAgent", onSelectAgent);
    window.removeEventListener("getstarted:focus-composer", onFocusComposer);
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
