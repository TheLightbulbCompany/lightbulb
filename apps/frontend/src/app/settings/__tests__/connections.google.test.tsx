import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

const connections: { provider: string; status: string }[] = [];

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@clerk/nextjs", () => ({ UserButton: () => <div /> }));
vi.mock("@/components/Logo", () => ({ Logo: () => <div /> }));
vi.mock("@/components/settings/SettingsNav", () => ({ SettingsNav: () => <nav /> }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections, mutate: vi.fn() }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({ CompanyStripeSection: () => <div /> }));
vi.mock("@/components/settings/CompanyAccountsSection", () => ({ CompanyAccountsSection: () => <div /> }));
vi.mock("@/components/settings/ConnectionsList", () => ({
  ConnectionsList: () => <div data-testid="connections-list" />,
}));
vi.mock("@/components/control/panels/GoogleAccountsSection", () => ({
  GoogleAccountsSection: () => <div data-testid="google-accounts" />,
}));

import ConnectionsPage from "../connections/page";

describe("/settings/connections — Google accounts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    connections.length = 0;
  });

  // The page a user opens to manage a connection used to show a connected
  // Google as one account with nothing but Disconnect — the multi-account and
  // per-agent surface lived only in the Control dashboard. It is also where
  // "connect another Google account" redirects BACK to, so this is its home.
  it("renders the multi-account surface when Google is connected", async () => {
    connections.push({ provider: "google", status: "connected" });
    render(<ConnectionsPage />);
    expect(await screen.findByTestId("google-accounts")).toBeInTheDocument();
  });

  it("stays hidden when Google is not connected", async () => {
    connections.push({ provider: "google", status: "disconnected" });
    render(<ConnectionsPage />);
    expect(await screen.findByTestId("connections-list")).toBeInTheDocument();
    expect(screen.queryByTestId("google-accounts")).toBeNull();
  });
});
