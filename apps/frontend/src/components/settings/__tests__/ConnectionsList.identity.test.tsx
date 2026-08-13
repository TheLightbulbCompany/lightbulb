import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { ConnectionsList } from "../ConnectionsList";
import { type Connection } from "@/hooks/useConnections";

vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport: vi.fn(), closeSupport: vi.fn() }),
}));

let rows: Connection[] = [];
vi.mock("@/hooks/useConnections", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useConnections")>()),
  useConnections: () => ({
    connections: rows,
    isLoading: false,
    error: null,
    mutate: vi.fn(),
    connectPaste: vi.fn(),
    startOAuth: vi.fn(),
    disconnect: vi.fn(),
  }),
}));

const base: Connection = {
  provider: "instagram",
  display_name: "Instagram",
  acquisition: "direct_oauth",
  status: "connected",
  actor: "cli",
};

describe("ConnectionsList — connected account identity", () => {
  it("names the account that is actually wired up", () => {
    // An owner can plausibly have several Instagram or Google accounts. Showing
    // "Connected" without saying WHICH one leaves them unable to tell a correct
    // connection from a wrong one.
    rows = [{ ...base, identity: "isol8.ai" }];
    render(<ConnectionsList canManage />);
    expect(screen.getByText("isol8.ai")).toBeInTheDocument();
  });

  it("omits the identity entirely when the row carries none", () => {
    // Connections made before identity was recorded have none. The row must read
    // cleanly rather than rendering a stray separator or an empty slot.
    rows = [{ ...base, identity: null }];
    const { container } = render(<ConnectionsList canManage />);
    expect(container.textContent).toContain("Connected");
    expect(container.textContent).not.toContain("· ·");
  });

  it("does not show an identity for a provider that is not connected", () => {
    // A stale identity on a disconnected row would imply a live credential.
    rows = [{ ...base, status: "disconnected", identity: "isol8.ai" }];
    render(<ConnectionsList canManage />);
    expect(screen.queryByText("isol8.ai")).not.toBeInTheDocument();
  });
});
