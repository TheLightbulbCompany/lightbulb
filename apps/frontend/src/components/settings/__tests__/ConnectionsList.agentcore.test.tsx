/**
 * M6 task 10-D, row 26. Shopify's `container_login` runs the CLI's OAuth grant
 * inside the owner's container and finishes it in the Browser panel — neither
 * exists on AgentCore.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { ConnectionsList } from "../ConnectionsList";
import type { Connection } from "@/hooks/useConnections";

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

let gate: "loading" | "hidden" | "shown";
let connections: Connection[];

vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});
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
    }),
  };
});
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({
    connections,
    isLoading: false,
    error: null,
    mutate: vi.fn(),
    connectPaste: vi.fn(),
    startOAuth: vi.fn(),
    startContainerLogin: vi.fn(),
    disconnect: vi.fn(),
  }),
}));

describe("ConnectionsList — Shopify container_login on AgentCore", () => {
  beforeEach(() => {
    connections = [
      { provider: "shopify", display_name: "Shopify", acquisition: "container_login", status: "disconnected" },
    ];
  });

  it("shows 'Not available on this runtime' and drops the Connect control once AgentCore is confirmed", () => {
    gate = "hidden";
    render(<ConnectionsList canManage />);
    expect(screen.getByText("Not available on this runtime")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect shopify/i })).toBeNull();
  });

  it("still offers Connect while the host isn't known yet", () => {
    gate = "loading";
    render(<ConnectionsList canManage />);
    expect(screen.queryByText("Not available on this runtime")).toBeNull();
    expect(screen.getByRole("button", { name: /connect shopify/i })).toBeInTheDocument();
  });

  it("is unaffected on ECS", () => {
    gate = "shown";
    render(<ConnectionsList canManage />);
    expect(screen.queryByText("Not available on this runtime")).toBeNull();
    expect(screen.getByRole("button", { name: /connect shopify/i })).toBeInTheDocument();
  });

  it("never fences a paste_token/direct_oauth provider — the hide is Shopify-specific", () => {
    gate = "hidden";
    connections = [
      { provider: "github", display_name: "GitHub", acquisition: "paste_token", status: "disconnected" },
    ];
    render(<ConnectionsList canManage />);
    expect(screen.queryByText("Not available on this runtime")).toBeNull();
    expect(screen.getByText(/not connected/i)).toBeInTheDocument();
  });
});
