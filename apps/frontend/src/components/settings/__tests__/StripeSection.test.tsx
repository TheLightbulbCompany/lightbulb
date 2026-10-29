import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";

import type { Connection } from "@/hooks/useConnections";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }), BACKEND_URL: "" }));

const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a>,
}));

const h = vi.hoisted(() => ({ connections: [] as Connection[] }));

vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({
    connections: h.connections,
    isLoading: false,
    error: null,
    mutate: vi.fn(),
    connectPaste: vi.fn(),
    startOAuth: vi.fn(),
    disconnect: vi.fn(),
  }),
  ApiError: class ApiError extends Error {},
}));

vi.mock("@/components/settings/ProvisionedResources", async () => {
  const actual = await vi.importActual<typeof import("../ProvisionedResources")>(
    "../ProvisionedResources",
  );
  return {
    ...actual,
    useProvisioningRail: () => ({
      byProvider: new Map(),
      workflowsByProvider: new Map(),
      accountByProvider: new Map(),
      providers: [],
      refresh: vi.fn(),
      error: null,
    }),
  };
});

import { StripeSection } from "../StripeSection";

const COMPANY_BOUND = {
  account_id: "acct_x",
  source: "created",
  details_submitted: true,
  charges_enabled: true,
  payouts_enabled: false,
  dashboard_url: "https://dashboard.stripe.com/",
  connect_configured: true,
};

const COMPANY_UNBOUND = {
  account_id: null,
  source: null,
  details_submitted: false,
  charges_enabled: false,
  payouts_enabled: false,
  dashboard_url: null,
  connect_configured: true,
};

// SWR has a global cache that persists across renders/tests — wrap each
// render in a fresh SWRConfig with `provider: () => new Map()` to isolate.
function renderSection() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <StripeSection canManage />
    </SWRConfig>,
  );
}

describe("StripeSection — the consolidated Stripe card", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    h.connections = [];
  });

  it("shows both grant states at a glance: company connected-as, agent tools not connected", async () => {
    get.mockResolvedValue(COMPANY_BOUND);
    h.connections = [
      { provider: "stripe", display_name: "Stripe", acquisition: "direct_oauth", status: "disconnected", actor: "mcp" },
    ];
    renderSection();

    // One brand header, two labeled grants.
    expect(await screen.findByRole("heading", { name: "Stripe" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Company account" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Agent tools" })).toBeInTheDocument();

    // Company grant: connected-as. Agent grant: its own state and its own action.
    expect(await screen.findByText("acct_x")).toBeInTheDocument();
    expect(screen.getByText(/Not connected · OAuth/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Stripe" })).toBeInTheDocument();

    // The same-account hint is copy only — it exists because the company
    // account is bound, never because any credential crosses the planes.
    expect(screen.getByText(/pick the same account you connected above/)).toBeInTheDocument();
  });

  it("offers the company doors and no same-account hint while the company account is unbound", async () => {
    get.mockResolvedValue(COMPANY_UNBOUND);
    h.connections = [
      { provider: "stripe", display_name: "Stripe", acquisition: "direct_oauth", status: "disconnected" },
    ];
    renderSection();

    expect(await screen.findByText("Set up Stripe")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Agent tools" })).toBeInTheDocument();
    expect(screen.queryByText(/pick the same account/)).toBeNull();
  });

  it("renders a connected agent grant with its own disconnect action", async () => {
    get.mockResolvedValue(COMPANY_BOUND);
    h.connections = [
      { provider: "stripe", display_name: "Stripe", acquisition: "direct_oauth", status: "connected", actor: "mcp" },
    ];
    renderSection();

    expect(await screen.findByText(/Connected · OAuth/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect Stripe" })).toBeInTheDocument();
    expect(screen.queryByText(/pick the same account/)).toBeNull();
  });

  it("shows a dead agent grant as needing a reconnect, and says why", async () => {
    get.mockResolvedValue(COMPANY_BOUND);
    h.connections = [
      {
        provider: "stripe",
        display_name: "Stripe",
        acquisition: "direct_oauth",
        status: "reconnect_required",
        reconnect_reason: "invalid_grant: Invalid refresh token",
      },
    ];
    renderSection();

    expect(
      await screen.findByText("Stripe ended the sign-in (invalid_grant). Reconnect to continue."),
    ).toBeInTheDocument();
    const status = screen.getByText(/Needs reconnecting · OAuth/);
    expect(screen.queryByText(/Not connected/)).toBeNull();
    expect(screen.getByRole("button", { name: "Reconnect Stripe" })).toBeInTheDocument();
    // The status dot sits beside the Agent tools text block; it must not read as healthy.
    const dot = status.closest(".flex.items-start")?.querySelector("span[aria-hidden='true']");
    expect(dot?.className).not.toContain("bg-[var(--ok)]");
  });

  it("keeps Disconnect but does not claim an unsupported old agent grant works", async () => {
    get.mockResolvedValue(COMPANY_UNBOUND);
    h.connections = [
      {
        provider: "stripe",
        display_name: "Stripe",
        acquisition: "direct_oauth",
        status: "connected",
        actor: "mcp",
        runtime_supported: false,
      },
    ];
    renderSection();

    expect(await screen.findByText(/Authorized · unavailable to agents/)).toBeInTheDocument();
    expect(screen.queryByText(/Delivered:/)).toBeNull();
    expect(screen.getByRole("button", { name: "Disconnect Stripe" })).toBeInTheDocument();
  });
});
