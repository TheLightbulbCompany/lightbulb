import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { ConnectionsList } from "../ConnectionsList";
import type { Connection } from "@/hooks/useConnections";

// Render next/link (used by ConnectButton's locked upsell) as a plain anchor.
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

// Mutable holders the hook mocks read from, so each test can vary the owner's
// provider_choice and the catalog rows without re-hoisting the mock.
const h = vi.hoisted(() => ({
  account: null as { provider_choice: string | null } | null,
  connections: [] as Connection[],
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: h.account }),
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

const googleRow: Connection = {
  provider: "google",
  display_name: "Google Workspace",
  acquisition: "direct_oauth",
  status: "disconnected",
  requires_bedrock: true,
};

describe("ConnectionsList — Bedrock lock", () => {
  beforeEach(() => {
    h.account = null;
    h.connections = [googleRow];
  });

  it("locks a requires_bedrock row with an upsell when the owner is NOT on Bedrock", () => {
    h.account = { provider_choice: "chatgpt" };
    render(<ConnectionsList canManage />);

    expect(screen.getByText("Requires Bedrock credits")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /switch to bedrock to unlock google workspace/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).toBeNull();
  });

  it("shows Connect (no lock) when the owner IS on Bedrock", () => {
    h.account = { provider_choice: "bedrock_claude" };
    render(<ConnectionsList canManage />);

    expect(screen.getByRole("button", { name: /connect google workspace/i })).toBeInTheDocument();
    expect(screen.queryByText("Requires Bedrock credits")).toBeNull();
    expect(screen.queryByRole("link", { name: /switch to bedrock/i })).toBeNull();
  });

  it("keeps Disconnect reachable for a connected row even when off Bedrock", () => {
    h.account = { provider_choice: "chatgpt" };
    h.connections = [{ ...googleRow, status: "connected" }];
    render(<ConnectionsList canManage />);

    expect(screen.getByRole("button", { name: /disconnect google workspace/i })).toBeInTheDocument();
    expect(screen.queryByText("Requires Bedrock credits")).toBeNull();
  });
});
