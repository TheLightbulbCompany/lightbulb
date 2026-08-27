import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { PluginsTab } from "../PluginsTab";

// useGatewayRpc is called twice — discriminate by method.
const mockConfig = vi.fn();
const mockCatalog = vi.fn();
const mockMutate = vi.fn();
const mockCallRpc = vi.fn();
const mockMembership = vi.fn();

vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string | null) => {
    if (method === "config.get") {
      return { data: mockConfig(), error: undefined, isLoading: false, mutate: mockMutate };
    }
    if (method === "tools.catalog") {
      return { data: mockCatalog(), error: undefined, isLoading: false, mutate: vi.fn() };
    }
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useGatewayRpcMutation: () => mockCallRpc,
}));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: mockMembership() }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

describe("PluginsTab", () => {
  beforeEach(() => {
    mockConfig.mockReset();
    mockCatalog.mockReset();
    mockMutate.mockReset();
    mockCallRpc.mockReset();
    mockMembership.mockReset();
    mockCatalog.mockReturnValue(undefined);
    mockMembership.mockReturnValue(null); // personal owner => admin
    mockCallRpc.mockResolvedValue(undefined);
  });

  it("lists configured plugins with enabled/disabled state", () => {
    mockConfig.mockReturnValue({
      config: {
        plugins: { entries: { "amazon-bedrock": {}, lobster: { enabled: true }, "custom-x": { enabled: false } } },
        agents: { list: [] },
      },
    });
    render(<PluginsTab />);
    expect(screen.getByText("amazon-bedrock")).toBeInTheDocument();
    expect(screen.getByText("custom-x")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
  });

  it("locks Lightbulb AI-managed plugins (Managed badge, no toggle button)", () => {
    mockConfig.mockReturnValue({
      config: { plugins: { entries: { lobster: { enabled: true } } }, agents: { list: [] } },
    });
    render(<PluginsTab />);
    expect(screen.getByText("lobster")).toBeInTheDocument();
    expect(screen.getByText("Managed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /disable/i })).toBeNull();
  });

  it("toggling an unmanaged plugin calls config.patch with a merge-patch raw payload", async () => {
    mockConfig.mockReturnValue({
      hash: "h1",
      config: { plugins: { entries: { "custom-x": { enabled: true } } }, agents: { list: [] } },
    });
    render(<PluginsTab />);
    fireEvent.click(screen.getByRole("button", { name: /disable/i }));
    await waitFor(() => expect(mockCallRpc).toHaveBeenCalledTimes(1));
    expect(mockCallRpc).toHaveBeenCalledWith("config.patch", {
      raw: JSON.stringify({ plugins: { entries: { "custom-x": { enabled: false } } } }),
      baseHash: "h1",
    });
  });

  it("surfaces an error when config.patch is rejected (e.g. stale base hash)", async () => {
    mockConfig.mockReturnValue({
      hash: "h1",
      config: { plugins: { entries: { "custom-x": { enabled: true } } }, agents: { list: [] } },
    });
    mockCallRpc.mockRejectedValueOnce(new Error("config changed since last load; re-run config.get and retry"));
    render(<PluginsTab />);
    fireEvent.click(screen.getByRole("button", { name: /disable/i }));
    await waitFor(() => expect(screen.getByText(/config changed since last load/i)).toBeInTheDocument());
  });

  it("hides toggles for non-admin org members", () => {
    mockMembership.mockReturnValue({ role: "org:member" });
    mockConfig.mockReturnValue({
      config: { plugins: { entries: { "custom-x": { enabled: true } } }, agents: { list: [] } },
    });
    render(<PluginsTab />);
    expect(screen.getByText("custom-x")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /disable/i })).toBeNull();
  });

  it("counts tools each plugin contributes from tools.catalog", () => {
    mockConfig.mockReturnValue({
      config: { plugins: { entries: { browser: {} } }, agents: { list: [{ id: "agent_1" }] } },
    });
    mockCatalog.mockReturnValue({
      groups: [
        {
          id: "g1",
          label: "Browser",
          source: "plugin",
          tools: [
            { name: "browse", source: "plugin", pluginId: "browser" },
            { name: "screenshot", source: "plugin", pluginId: "browser" },
          ],
        },
      ],
    });
    render(<PluginsTab />);
    expect(screen.getByText(/Provides 2 tools/)).toBeInTheDocument();
  });

  it("includes plugins discovered via tools.catalog even when absent from config.plugins.entries", () => {
    mockConfig.mockReturnValue({
      config: { plugins: { entries: {} }, agents: { list: [{ id: "agent_1" }] } },
    });
    mockCatalog.mockReturnValue({
      groups: [
        {
          id: "g1",
          label: "Browser",
          source: "plugin",
          tools: [{ name: "browse", source: "plugin", pluginId: "browser" }],
        },
      ],
    });
    render(<PluginsTab />);
    expect(screen.getByText("browser")).toBeInTheDocument();
    // Discovered-but-unconfigured plugins default to enabled.
    expect(screen.getByText("Enabled")).toBeInTheDocument();
  });

  it("hides channel plugins — the Channels panel owns their lifecycle", () => {
    mockConfig.mockReturnValue({
      config: {
        plugins: { entries: { slack: { enabled: true }, discord: {}, telegram: {}, googlechat: {}, "custom-x": {} } },
        agents: { list: [] },
      },
    });
    render(<PluginsTab />);
    expect(screen.queryByText("slack")).toBeNull();
    expect(screen.queryByText("discord")).toBeNull();
    expect(screen.queryByText("telegram")).toBeNull();
    expect(screen.queryByText("googlechat")).toBeNull();
    expect(screen.getByText("custom-x")).toBeInTheDocument();
    expect(screen.getByText(/managed in the channels panel/i)).toBeInTheDocument();
  });
});
