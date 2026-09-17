/**
 * M6 task 10-D, row 13. Hermes has no reader for `config.plugins.entries` —
 * the Advanced disclosure (the only route to PluginsTab) is dropped outright
 * on a confirmed AgentCore workspace, in both the Control-dashboard and
 * Settings-embedded variants.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { IntegrationsPanel } from "../IntegrationsPanel";

let gate: "loading" | "hidden" | "shown";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [] }) }));
vi.mock("@/components/settings/ConnectionsList", () => ({ ConnectionsList: () => <div /> }));
vi.mock("@/components/control/panels/GoogleAccountsSection", () => ({ GoogleAccountsSection: () => <div /> }));
vi.mock("@/components/control/panels/McpServersTab", () => ({ McpServersTab: () => <div /> }));
vi.mock("@/components/control/panels/PluginsTab", () => ({ PluginsTab: () => <div data-testid="plugins-tab" /> }));

describe("IntegrationsPanel — Advanced/plugins on AgentCore", () => {
  beforeEach(() => {
    gate = "hidden";
  });

  it("drops the Advanced disclosure (control variant) once AgentCore is confirmed", () => {
    render(<IntegrationsPanel />);
    expect(screen.queryByRole("button", { name: /advanced — plugins/i })).toBeNull();
  });

  it("drops it in the Settings-embedded variant too", () => {
    render(<IntegrationsPanel variant="settings" />);
    expect(screen.queryByRole("button", { name: /advanced — plugins/i })).toBeNull();
  });

  it("still offers the disclosure while the host isn't known yet", () => {
    gate = "loading";
    render(<IntegrationsPanel />);
    expect(screen.getByRole("button", { name: /advanced — plugins/i })).toBeInTheDocument();
  });

  it("is unaffected on ECS", () => {
    gate = "shown";
    render(<IntegrationsPanel />);
    expect(screen.getByRole("button", { name: /advanced — plugins/i })).toBeInTheDocument();
  });
});
