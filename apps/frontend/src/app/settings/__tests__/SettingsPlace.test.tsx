import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";

// UX slice 4: Settings as a place in the shell (super-agent), and /settings sending old URLs there.

// Mutable ?panel= param so deep-link-guard tests can drive the initial panel.
const nav = vi.hoisted(() => ({ panel: null as string | null, extra: "", replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => {
    const p = new URLSearchParams(nav.extra);
    if (nav.panel) p.set("panel", nav.panel);
    return p;
  },
  useRouter: () => ({ replace: nav.replace }),
  usePathname: () => "/settings",
}));

// The Settings page composes several gateway-bound Control panels. Stub them all
// to inert markers so this nav-structure test never opens a WebSocket or fires
// an RPC — we only assert the navigation renders, not panel internals.
vi.mock("@/components/control/panels/UsagePanel", () => ({
  UsagePanel: () => <div data-testid="usage-panel" />,
}));
vi.mock("@/components/control/panels/LLMPanel", () => ({
  LLMPanel: ({ initialAction }: { initialAction?: string | null }) => (
    <div data-testid="llm-panel" data-action={initialAction ?? ""} />
  ),
}));
vi.mock("@/components/control/panels/ConnectionsPanel", () => ({
  ConnectionsPanel: () => <div id="panel-connections" data-testid="connections-panel" />,
}));
vi.mock("@/components/teams/panels/SettingsPanel", () => ({
  SettingsPanel: () => <div data-testid="company-settings" />,
}));
vi.mock("@/components/control/panels/IntegrationsPanel", () => ({
  IntegrationsPanel: () => <div data-testid="integrations-panel" />,
}));
vi.mock("@/components/control/panels/SkillsTab", () => ({
  SkillsTab: () => <div data-testid="skills-tab" />,
}));

// Mutable membership holder so individual tests can flip admin ↔ non-admin.
// `null` = personal owner = admin (the `useIsWorkspaceAdmin` personal branch);
// `{ role: "org:member" }` = a non-admin org member (Usage must hide).
const clerk = vi.hoisted(() => ({
  membership: null as { role: string } | null,
  isLoaded: true,
}));

vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: {
      fullName: "Test User",
      firstName: "Test",
      lastName: "User",
      primaryEmailAddress: { emailAddress: "test@isol8.co" },
    },
  }),
  useAuth: () => ({ getToken: () => Promise.resolve("jwt"), isLoaded: true, userId: "user_sam", orgId: "org_1" }),
  useOrganization: () => ({ membership: clerk.membership, organization: null, isLoaded: clerk.isLoaded }),
  useClerk: () => ({ signOut: vi.fn() }),
  UserButton: () => <div data-testid="user-button" />,
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: null,
    isLoading: false,
    error: null,
    isSubscribed: false,
    openPortal: vi.fn(),
  }),
}));

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ tasks: { cacheScope: "org_test" } }),
  useOptionalLiveEvents: () => ({ httpReady: true }),
}));
const shell = vi.hoisted(() => ({ openPanel: vi.fn(), updatePanelParams: vi.fn(), replaceSearchParams: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: shell.openPanel, updatePanelParams: shell.updatePanelParams }));
vi.mock("@/lib/shellNav", async (importOriginal) => ({ ...(await importOriginal<object>()), replaceSearchParams: shell.replaceSearchParams }));
vi.mock("@/components/teams/panels/MoneyPanel", () => ({
  MoneyPanel: ({ embedded }: { embedded?: boolean }) => <div data-testid="money-panel" data-embedded={String(!!embedded)} />,
}));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(async () => ({})) }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), useApi: () => api }));
// The builtin jobs belong to the workspace (owner id = the org), as the server stores them.
const job = (id: string, status: string) => ({ id, name: id, status, ownerUserId: "org_1", deliverTo: "none", match: null });

import { SWRConfig } from "swr";
import SettingsPage from "../page";
import { SettingsPlace } from "@/components/settings/SettingsPlace";

const place = (params: Record<string, string> = {}) => render(
  <SWRConfig value={{ provider: () => new Map() }}><SettingsPlace id="settings" params={params} /></SWRConfig>);

beforeEach(() => {
  clerk.membership = null;
  nav.panel = null; nav.extra = ""; nav.replace.mockReset();
  shell.openPanel.mockReset(); shell.replaceSearchParams.mockReset(); shell.updatePanelParams.mockReset();
  api.get.mockReset(); api.post.mockClear();
  api.get.mockImplementation(async (path: string) => {
    if (path === "/automations") return { automations: [job("builtin:look_around", "active"), job("builtin:feed", "paused"), job("a1", "active")] };
    throw new Error(`no route ${path}`);
  });
});

describe("Settings place (super-agent)", () => {
  it("Frame A: titled Settings, four plain groups, 9 rows; Business under Plan; no Budgets or Marketplace; no Developer group yet", () => {
    place();
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    const navEl = screen.getByRole("navigation", { name: "Settings navigation" });
    expect([...navEl.querySelectorAll(".settings-nav-section-label")].map((l) => l.textContent)).toEqual(["You", "Workspace", "Connect", "Plan"]);
    const rows = within(navEl).getAllByRole("tab").map((t) => t.textContent);
    expect(rows).toEqual(["Profile", "Workspace", "Members", "Approvals", "Connections", "Billing", "Usage", "Business", "Model & credits"]);
    // No route chrome: no top bar, no Back to Chat.
    expect(screen.queryByRole("link", { name: /Back to Chat/ })).toBeNull();
  });

  it("the Developer group shows once the workspace has its own MCP server, or for a link to one of its rows (W13-10)", async () => {
    const devRows = () => [...screen.getByRole("navigation", { name: "Settings navigation" }).querySelectorAll(".settings-nav-section-label")].map((l) => l.textContent);
    const { unmount } = place({ section: "mcp" });
    expect(devRows()).toContain("Developer");
    expect(screen.getByRole("tab", { name: /^MCP servers/ })).toHaveAttribute("aria-selected", "true");
    // A browser (no desktop app) has no local MCP servers: neither a tab nor a select option.
    expect(screen.queryByRole("tab", { name: /Local MCP servers/ })).toBeNull();
    expect(screen.queryByRole("option", { name: "Local MCP servers" })).toBeNull();
    unmount();
    // Only Lightbulb's managed server: still hidden.
    api.get.mockImplementation(async (path: string) => {
      if (path === "/integrations/mcp/servers") return { servers: { lightbulb: {} }, managed_keys: ["lightbulb"] };
      throw new Error(`no route ${path}`);
    });
    const second = place();
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith("/integrations/mcp/servers"));
    expect(devRows()).not.toContain("Developer");
    second.unmount();
    api.get.mockImplementation(async (path: string) => {
      if (path === "/integrations/mcp/servers") return { servers: { lightbulb: {}, github: {} }, managed_keys: ["lightbulb"] };
      throw new Error(`no route ${path}`);
    });
    place();
    await vi.waitFor(() => expect(devRows()).toEqual(["You", "Workspace", "Connect", "Plan", "Developer"]));
  });

  it("Profile is rows, not a card per setting: Edit profile beside who you are, no session notifications (W13-10)", async () => {
    api.get.mockImplementation(async (path: string) => {
      if (path === "/users/me/prefs") return { timezone: "UTC", dailySummary: true, awayEmail: true };
      throw new Error(`no route ${path}`);
    });
    const { container } = place();
    expect(await screen.findByRole("switch", { name: "Daily summary" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit profile" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete…" })).toBeInTheDocument();
    expect(container.querySelector("#panel-profile .settings-card")).toBeNull();
    expect(screen.queryByText("Session notifications")).toBeNull();
    expect(screen.queryByText(/account menu at the bottom left/)).toBeNull();
  });

  it("a row opens its section in place (the URL replaced, not pushed); Business is Money without its masthead", () => {
    place({ section: "business" });
    expect(screen.getByRole("tab", { name: /Business/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("money-panel")).toHaveAttribute("data-embedded", "true");
    fireEvent.click(screen.getByRole("tab", { name: /Billing/ }));
    expect(shell.openPanel).toHaveBeenCalledWith("settings", { section: "billing" });
    expect(shell.replaceSearchParams).toHaveBeenCalledWith(new URLSearchParams({ panel: "settings", section: "billing" }));
  });

  it("the shell's one-shot action reaches Model & credits", () => {
    place({ section: "providers", action: "change-provider" });
    expect(screen.getByTestId("llm-panel")).toHaveAttribute("data-action", "change-provider");
  });

  it("Workspace: Daily look around and Feed posts switches pause and resume the builtin jobs (an org admin)", async () => {
    clerk.membership = { role: "org:admin" };
    place({ section: "workspace" });
    const look = await screen.findByRole("switch", { name: "Daily look around" });
    const feed = screen.getByRole("switch", { name: "Feed posts" });
    expect(look).toHaveAttribute("aria-checked", "true");
    expect(feed).toHaveAttribute("aria-checked", "false");
    fireEvent.click(look);
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/automations/builtin%3Alook_around/pause", {}));
    fireEvent.click(feed);
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/automations/builtin%3Afeed/resume", {}));
  });

  it("a member can't resume a workspace's builtin job (the server's _may_activate)", async () => {
    clerk.membership = { role: "org:member" };
    place({ section: "workspace" });
    // Workspace is admin-free; the switch is there but off-limits to resume.
    expect(await screen.findByRole("switch", { name: "Feed posts" })).toBeDisabled();
  });

  it("a link to a row this person can't open says why, in one line, rather than silently opening Profile", () => {
    clerk.membership = { role: "org:member" };
    const { unmount } = place({ section: "usage" });
    expect(screen.getByRole("status")).toHaveTextContent("Usage is for workspace admins. Ask one to open it.");
    expect(screen.getByRole("tab", { name: /Profile/ })).toHaveAttribute("aria-selected", "true");
    unmount();
  });

  it("/settings redirects into the shell with its section and every other param", () => {
    nav.panel = "connections"; nav.extra = "connected=github";
    render(<SettingsPage />);
    expect(nav.replace).toHaveBeenCalledWith("/chat?connected=github&panel=settings&section=connections");
  });
});
