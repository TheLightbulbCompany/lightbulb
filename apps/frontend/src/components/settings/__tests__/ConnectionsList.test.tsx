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
}));

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

describe("ConnectionsList — delivery receipts", () => {
  beforeEach(() => {
    h.connections = [];
  });

  it("shows what a connected provider delivered", () => {
    h.connections = [
      { provider: "stripe", display_name: "Stripe", acquisition: "paste_token", status: "connected" },
    ];
    render(<ConnectionsList canManage />);

    expect(screen.getByText(/delivered/i)).toBeInTheDocument();
    expect(screen.getByText(/payment tools/i)).toBeInTheDocument();
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
  beforeEach(() => {
    h.connections = [];
  });

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
  beforeEach(() => {
    h.connections = [];
  });

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
