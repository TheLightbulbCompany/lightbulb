import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { ConnectionsList } from "../ConnectionsList";
import type { Connection } from "@/hooks/useConnections";

// Render next/link (used by ConnectButton's BYO setup link) as a plain anchor.
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

// Mutable holder the hook mock reads from, so each test can vary the catalog
// rows without re-hoisting.
const h = vi.hoisted(() => ({
  connections: [] as Connection[],
  resources: new Map<string, unknown[]>(),
  workflows: new Map<string, unknown[]>(),
  accounts: new Map<string, unknown>(),
}));

// The Projects rail's facet does its own reads; the list only groups them.
vi.mock("@/components/settings/ProvisionedResources", async () => {
  const actual = await vi.importActual<typeof import("../ProvisionedResources")>(
    "../ProvisionedResources",
  );
  return {
    ...actual,
    useProvisioningRail: () => ({
      byProvider: h.resources,
      workflowsByProvider: h.workflows,
      accountByProvider: h.accounts,
      providers: [...new Set([...h.resources.keys(), ...h.workflows.keys()])].sort(),
      refresh: vi.fn(),
    }),
  };
});

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
}));

function resetRail() {
  h.connections = [];
  h.resources = new Map();
  h.workflows = new Map();
  h.accounts = new Map();
}

describe("ConnectionsList — delivery receipts", () => {
  beforeEach(resetRail);

  it("shows what a connected provider delivered", () => {
    h.connections = [
      { provider: "stripe", display_name: "Stripe", acquisition: "paste_token", status: "connected" },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/delivered/i)).toBeInTheDocument();
    expect(screen.getByText(/payment tools/i)).toBeInTheDocument();
  });

  it("does not present an unsupported old grant as usable by agents", () => {
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
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/authorized.*unavailable to agents/i)).toBeInTheDocument();
    expect(screen.queryByText(/delivered:/i)).toBeNull();
    expect(screen.getByRole("button", { name: "Disconnect Stripe" })).toBeInTheDocument();
  });

  it("says why a grant needs a reconnect", () => {
    h.connections = [
      {
        provider: "linear",
        display_name: "Linear",
        acquisition: "direct_oauth",
        status: "reconnect_required",
        reconnect_reason: "no_refresh_token",
      },
    ];
    render(<ConnectionsList canManage />);

    expect(
      screen.getByText("The saved sign-in can't be renewed (no_refresh_token). Reconnect to continue."),
    ).toBeInTheDocument();
  });

  it("shows what a disconnected provider will deliver", () => {
    h.connections = [
      { provider: "github", display_name: "GitHub", acquisition: "paste_token", status: "disconnected" },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/will deliver/i)).toBeInTheDocument();
  });

  it("renders unknown providers without a receipt line", () => {
    h.connections = [
      { provider: "acme", display_name: "Acme", acquisition: "paste_token", status: "connected" },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.queryByText(/will deliver|delivered/i)).toBeNull();
  });
});

describe("ConnectionsList — inbound trigger capability", () => {
  beforeEach(resetRail);

  it("shows a Can trigger line when the provider declares inbound events", () => {
    h.connections = [
      {
        provider: "stripe",
        display_name: "Stripe",
        acquisition: "direct_oauth",
        status: "connected",
        can_trigger: ["Invoice payment failed", "Charge refunded"],
      },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/can trigger/i)).toBeInTheDocument();
    expect(screen.getByText(/invoice payment failed, charge refunded/i)).toBeInTheDocument();
  });

  it("truncates the Can trigger line to three events plus a count", () => {
    // Full catalogs now — a Stripe row carries ~265 labels; the card shows
    // the curated leaders and counts the rest instead of a wall of text.
    h.connections = [
      {
        provider: "stripe",
        display_name: "Stripe",
        acquisition: "direct_oauth",
        status: "connected",
        can_trigger: ["One", "Two", "Three", "Four", "Five"],
      },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/can trigger: one, two, three and 2 more/i)).toBeInTheDocument();
    expect(screen.queryByText(/four/i)).toBeNull();
  });

  it("renders no Can trigger line when the provider has no inbound events", () => {
    h.connections = [
      {
        provider: "acme",
        display_name: "Acme",
        acquisition: "paste_token",
        status: "connected",
        can_trigger: [],
      },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.queryByText(/can trigger/i)).toBeNull();
  });
});

describe("ConnectionsList — integration kind badges", () => {
  beforeEach(resetRail);

  it("badges an MCP integration as MCP tools", () => {
    h.connections = [
      {
        provider: "stripe",
        display_name: "Stripe",
        acquisition: "direct_oauth",
        status: "connected",
        actor: "mcp",
      },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText("MCP tools")).toBeInTheDocument();
    // MCP integrations DO surface as tools — no shell explainer.
    expect(screen.queryByText(/command line/i)).toBeNull();
  });

  it("badges a CLI integration and explains it never appears as tools", () => {
    h.connections = [
      {
        provider: "github",
        display_name: "GitHub",
        acquisition: "direct_oauth",
        status: "connected",
        actor: "cli",
      },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText("CLI")).toBeInTheDocument();
    // The load-bearing line: a connected CLI integration must not read as a
    // broken MCP one — its absence from the agent's tool list is by design.
    expect(screen.getByText(/won.t appear in the agent.s tool list/i)).toBeInTheDocument();
  });

  it("renders no badge when the backend sends no actor (older API)", () => {
    h.connections = [
      { provider: "acme", display_name: "Acme", acquisition: "paste_token", status: "connected" },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.queryByText("MCP tools")).toBeNull();
    expect(screen.queryByText("CLI")).toBeNull();
    expect(screen.queryByText("Skill")).toBeNull();
  });
});

// Two rails, one card (spec §3.2): the OAuth relationship and the resources the
// Projects rail created on the owner's own provider identity.
describe("ConnectionsList — the Projects resources facet", () => {
  beforeEach(resetRail);

  const RESOURCE = {
    resource_id: "res_1",
    provider: "posthog",
    service: "analytics",
    status: "active",
    resource_name: "Northwind analytics",
    env_keys: ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    level: "company",
  };

  it("hangs the resources off the provider's own card, beside the OAuth facet", () => {
    h.connections = [
      { provider: "posthog", display_name: "PostHog", acquisition: "direct_oauth", status: "connected" },
    ];
    h.resources = new Map([["posthog", [RESOURCE]]]);
    render(<ConnectionsList canManage />);

    expect(screen.getByText("PostHog")).toBeInTheDocument();
    expect(screen.getByText("Northwind analytics")).toBeInTheDocument();
    expect(screen.getByText("Company-level")).toBeInTheDocument();
  });

  it("offers the read side when the provider has resources but no OAuth connection", () => {
    h.connections = [
      { provider: "posthog", display_name: "PostHog", acquisition: "direct_oauth", status: "disconnected" },
    ];
    h.resources = new Map([["posthog", [RESOURCE]]]);
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/Also connect this account for reads/)).toBeInTheDocument();
  });

  it("names the account this rail set up, so the owner knows which login to use", () => {
    h.connections = [
      { provider: "posthog", display_name: "PostHog", acquisition: "direct_oauth", status: "disconnected" },
    ];
    h.resources = new Map([["posthog", [RESOURCE]]]);
    h.accounts = new Map([["posthog", { display_name: "Northwind", primary_email: "ops@northwind.test" }]]);
    render(<ConnectionsList canManage />);

    expect(
      screen.getByText(/The account we set up is Northwind · ops@northwind\.test\./),
    ).toBeInTheDocument();
  });

  it("says nothing about reads when the provider is already connected", () => {
    h.connections = [
      { provider: "posthog", display_name: "PostHog", acquisition: "direct_oauth", status: "connected" },
    ];
    h.resources = new Map([["posthog", [RESOURCE]]]);
    render(<ConnectionsList canManage />);

    expect(screen.queryByText(/Also connect this account for reads/)).toBeNull();
  });

  it("gives a card to a provider the OAuth registry has never heard of", () => {
    h.resources = new Map([
      ["neon", [{ ...RESOURCE, provider: "neon", resource_name: "Primary", service: "postgres" }]],
    ]);
    render(<ConnectionsList canManage />);

    expect(screen.getByText("neon")).toBeInTheDocument();
    expect(screen.getByText("Primary")).toBeInTheDocument();
    // Nothing to connect: it is not in the OAuth registry.
    expect(screen.queryByRole("button", { name: /connect/i })).toBeNull();
    expect(screen.getByText(/Set up by your agents/)).toBeInTheDocument();
  });
});

// The two "dashboard access" paste rows (attio_api, apollo_api) asked for a
// second key nothing reads. The registry keeps them; the list does not offer them.
describe("ConnectionsList — dashboard-access paste rows", () => {
  beforeEach(resetRail);

  it("lists neither attio_api nor apollo_api, and keeps their agent rows", () => {
    h.connections = [
      { provider: "attio", display_name: "Attio", acquisition: "paste_token", status: "disconnected" },
      { provider: "attio_api", display_name: "Attio (dashboard access)", acquisition: "paste_token", status: "disconnected" },
      { provider: "apollo_api", display_name: "Apollo (dashboard access)", acquisition: "paste_token", status: "connected" },
    ];
    render(<ConnectionsList canManage />);
    expect(screen.getByText("Attio")).toBeInTheDocument();
    expect(screen.queryByText(/dashboard access/)).toBeNull();
  });
});
