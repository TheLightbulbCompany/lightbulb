// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

const connections: { provider: string; status: string }[] = [];
const mutate = vi.fn();
const replace = vi.fn();
let search = "";

// `router` must be ONE object for the life of the test: the real
// useRouter() is identity-stable, and an effect that depends on it re-fires on
// every render if the mock hands back a fresh literal each call.
const router = { replace };
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(search),
  usePathname: () => "/settings",
}));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections, mutate }),
}));
vi.mock("@/components/settings/StripeSection", () => ({ StripeSection: () => <div /> }));
vi.mock("@/components/settings/ConnectionsList", () => ({
  ConnectionsList: () => <div data-testid="connections-list" />,
}));
vi.mock("@/components/control/panels/GoogleAccountsSection", () => ({
  GoogleAccountsSection: () => <div data-testid="google-accounts" />,
}));
vi.mock("@/components/control/panels/WebsiteLoginsSection", () => ({
  WebsiteLoginsSection: () => <div />,
}));
vi.mock("@/components/control/panels/BrowserExtensionsSection", () => ({
  BrowserExtensionsSection: () => <div />,
}));
vi.mock("@/components/control/panels/AgentKeysSection", () => ({
  AgentKeysSection: () => <div />,
}));

import { ConnectionsPanel } from "../ConnectionsPanel";

describe("ConnectionsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    connections.length = 0;
    search = "?panel=connections";
  });

  // The surface a user opens to manage a connection used to show a connected
  // Google as one account with nothing but Disconnect — the multi-account and
  // per-agent surface lived only in the Control dashboard. It is also where
  // "connect another Google account" redirects BACK to, so this is its home.
  it("renders the multi-account surface when Google is connected", async () => {
    connections.push({ provider: "google", status: "connected" });
    render(<ConnectionsPanel />);
    expect(await screen.findByTestId("google-accounts")).toBeInTheDocument();
  });

  it("stays hidden when Google is not connected", async () => {
    connections.push({ provider: "google", status: "disconnected" });
    render(<ConnectionsPanel />);
    expect(await screen.findByTestId("connections-list")).toBeInTheDocument();
    expect(screen.queryByTestId("google-accounts")).toBeNull();
  });

  // The OAuth/Stripe return params land on Settings → Connections (the old
  // page is a redirector), so this panel owns the banner AND the strip.
  it("announces an OAuth return, refreshes the list, and strips the params", () => {
    search = "?panel=connections&connected=Notion";
    render(<ConnectionsPanel />);
    expect(screen.getByRole("status")).toHaveTextContent("Connected Notion.");
    expect(mutate).toHaveBeenCalled();
    // The strip keeps every other param — losing ?panel would close the panel
    // that is showing the banner.
    expect(replace).toHaveBeenCalledWith("/settings?panel=connections");
    // ONCE. useSearchParams() hands back a fresh object each render, so an
    // effect keyed on that object re-fires on the re-render its own setBanner
    // causes and loops until the heap dies. Keyed on the query string it runs
    // once per URL. This count is the guard.
    expect(replace).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("explains a failed OAuth return without refreshing the list", () => {
    search = "?panel=connections&error=oauth_state";
    render(<ConnectionsPanel />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "We couldn't verify the sign-in. Please try connecting again.",
    );
    expect(mutate).not.toHaveBeenCalled();
    expect(replace).toHaveBeenCalledWith("/settings?panel=connections");
  });

  it("announces a Stripe Connect return", () => {
    search = "?panel=connections&stripe=return&stripe_connect=connected";
    render(<ConnectionsPanel />);
    expect(screen.getByRole("status")).toHaveTextContent("Connected your Stripe account.");
    expect(replace).toHaveBeenCalledWith("/settings?panel=connections");
  });

  // `stripe=refresh` means the hosted onboarding link expired. There is nothing
  // to announce — StripeSection's own button mints a fresh one — but the param
  // must still be stripped so a reload doesn't re-enter this branch.
  it("strips an expired-link return without showing a banner", () => {
    search = "?panel=connections&stripe=refresh";
    render(<ConnectionsPanel />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(replace).toHaveBeenCalledWith("/settings?panel=connections");
  });

  // How agents ask moved to its own Settings › Approvals page (UX audit slice 2).
  it("renders as a Settings section, without the approval settings", () => {
    const { container } = render(<ConnectionsPanel />);
    expect(container.querySelector("#panel-connections")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Connections" })).toBeInTheDocument();
    expect(screen.getByTestId("connections-list")).toBeInTheDocument();
    expect(screen.queryByText("How agents ask")).toBeNull();
  });

  it("leaves the URL alone when there is no return to handle", () => {
    render(<ConnectionsPanel />);
    expect(replace).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
