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
vi.mock("@/components/control/panels/ConnectionsPanel", () => ({
  ConnectionsPanel: () => <div id="panel-connections" data-testid="connections-panel" />,
}));
vi.mock("@/components/teams/panels/SettingsPanel", () => ({
  SettingsPanel: () => <div data-testid="company-settings" />,
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
    if (path === "/automations") return { automations: [job("a1", "active")] };
    throw new Error(`no route ${path}`);
  });
});

describe("Settings place (super-agent)", () => {
  it("Frame A: titled Settings, four plain groups, 8 rows; Business under Plan; no Model, Developer, Budgets or Marketplace", () => {
    place();
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    const navEl = screen.getByRole("navigation", { name: "Settings navigation" });
    expect([...navEl.querySelectorAll(".settings-nav-section-label")].map((l) => l.textContent)).toEqual(["You", "Workspace", "Connect", "Plan"]);
    const rows = within(navEl).getAllByRole("tab").map((t) => t.textContent);
    expect(rows).toEqual(["Profile", "Workspace", "Members", "Approvals", "Connections", "Billing", "Usage", "Business"]);
    // No route chrome: no top bar, no Back to Chat.
    expect(screen.queryByRole("link", { name: /Back to Chat/ })).toBeNull();
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

  it("Workspace has no Daily look around or Feed posts switches (voice pivot)", async () => {
    clerk.membership = { role: "org:admin" };
    place({ section: "workspace" });
    expect(await screen.findByTestId("company-settings")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Daily look around" })).toBeNull();
    expect(screen.queryByRole("switch", { name: "Feed posts" })).toBeNull();
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
