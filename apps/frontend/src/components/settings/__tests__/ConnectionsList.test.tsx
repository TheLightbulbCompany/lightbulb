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
