import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";

// The one Settings page (product simplification, 2026-10-14): Control's panels,
// Team Settings and Accounts live here, in six intent groups. This test asserts
// the nav structure and that every section is deep-linkable.

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
  useAuth: () => ({ getToken: () => Promise.resolve("jwt"), isLoaded: true }),
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

import SettingsPage from "../page";

describe("Settings nav — the six groups of the one Settings page", () => {
  beforeEach(() => {
    // The Profile panel's prefs cards (SessionNotificationsCard, DailySummaryCard) read
    // /users/me/prefs through SWR, which fetched localhost:8000 for real. On a slow runner
    // the refused connection landed after jsdom was torn down and SWR's error path touched
    // `document` ("document is not defined"). A request that never answers keeps this
    // nav-structure test off the network.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    clerk.membership = null; // default: personal owner = admin
    clerk.isLoaded = true;
    nav.panel = null;
    nav.extra = "";
    nav.replace.mockClear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("renders the six group headers in order", () => {
    const { container } = render(<SettingsPage />);
    // Scope to the section-label nodes only so a nav *item* whose name
    // echoes a group header can never collide with the header text.
    const labels = Array.from(
      container.querySelectorAll(".settings-nav-section-label"),
    ).map((el) => el.textContent?.toUpperCase());
    expect(labels).toEqual([
      "YOU",
      "WORKSPACE",
      "CONNECTIONS",
      "PLAN AND USAGE",
      "MODEL",
      "ADVANCED",
    ]);
  });

  it("buckets the existing panels into their new groups", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Profile")).toBeInTheDocument(); // YOU
    expect(within(navEl).getByText("Workspace")).toBeInTheDocument(); // WORKSPACE
    expect(within(navEl).getByText("Connections")).toBeInTheDocument(); // CONNECTIONS
    expect(within(navEl).getByText("Billing")).toBeInTheDocument(); // PLAN AND USAGE
    expect(within(navEl).getByText("Desktop")).toBeInTheDocument(); // ADVANCED
    expect(within(navEl).getByText("Marketplace")).toBeInTheDocument(); // ADVANCED
  });

  it("renders Desktop as an icon-bearing action rather than a section label", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    const desktopRow = within(navEl).getByText("Desktop").closest("li");

    expect(desktopRow).not.toBeNull();
    expect(desktopRow?.querySelector("svg")).toBeInTheDocument();
  });

  // --- Usage (admin-gated, BILLING & USAGE) ---
  it("shows the admin-gated Usage row for an admin owner", () => {
    clerk.membership = null; // personal owner = admin
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Usage")).toBeInTheDocument();
  });

  it("hides the Usage row from a non-admin org member", () => {
    clerk.membership = { role: "org:member" };
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).queryByText("Usage")).not.toBeInTheDocument();
  });

  // --- Members (admin-gated, WORKSPACE) — the workspace invite door ---
  it("registers the Members row in WORKSPACE for an admin", () => {
    clerk.membership = null; // personal owner = admin
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Members")).toBeInTheDocument();
  });

  it("hides the Members row from a non-admin org member", () => {
    clerk.membership = { role: "org:member" };
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).queryByText("Members")).not.toBeInTheDocument();
  });

  it("honors a ?panel=members deep link for an admin", () => {
    nav.panel = "members";
    const { container } = render(<SettingsPage />);
    expect(container.querySelector("#panel-members")).toBeInTheDocument();
  });

  it("falls back to Profile for ?panel=members as a non-admin member", () => {
    clerk.membership = { role: "org:member" };
    nav.panel = "members";
    const { container } = render(<SettingsPage />);
    expect(container.querySelector("#panel-members")).not.toBeInTheDocument();
    expect(container.querySelector("#panel-profile")).toBeInTheDocument();
  });

  it("never opens an admin-only deep link while the membership is still loading", () => {
    // Clerk reports no membership at all until it has loaded.
    clerk.isLoaded = false;
    clerk.membership = null;
    nav.panel = "members";
    const { container, rerender } = render(<SettingsPage />);
    expect(container.querySelector("#panel-profile")).toBeInTheDocument();
    clerk.isLoaded = true;
    clerk.membership = { role: "org:member" };
    rerender(<SettingsPage />);
    expect(container.querySelector("#panel-members")).not.toBeInTheDocument();
    expect(container.querySelector("#panel-profile")).toBeInTheDocument();
  });

  it("opens an admin's deep link once the membership resolves", () => {
    clerk.isLoaded = false;
    clerk.membership = null;
    nav.panel = "members";
    const { container, rerender } = render(<SettingsPage />);
    expect(container.querySelector("#panel-members")).not.toBeInTheDocument();
    clerk.isLoaded = true;
    clerk.membership = { role: "org:admin" };
    rerender(<SettingsPage />);
    expect(container.querySelector("#panel-members")).toBeInTheDocument();
  });

  // --- Model & Credits (LLMPanel, MODEL & PROVIDERS group) ---
  it("registers the Model & Credits row and renders LLMPanel on select", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    const row = within(navEl).getByText("Model & Credits");
    expect(row).toBeInTheDocument();
    // No separate Credits row — CreditsSection is embedded inside LLMPanel.
    expect(within(navEl).queryByText("Credits")).not.toBeInTheDocument();
  });

  // --- MCP servers + Skills (ADVANCED) ---
  it("registers the MCP servers row in ADVANCED", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("MCP servers")).toBeInTheDocument();
  });

  it("registers the Skills row in ADVANCED", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Skills")).toBeInTheDocument();
  });

  // Channel wiring, channel status and Dreams / Memory were OpenClaw-shaped
  // surfaces with no equivalent on this runtime — the rows are GONE, not gated,
  // and a stale deep link falls back to Profile like any other unknown key.
  it.each(["Channels", "Channel Status", "Dreams / Memory", "Webhooks", "Skill Proposals"])(
    "offers no %s row",
    (label) => {
      render(<SettingsPage />);
      const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
      expect(within(navEl).queryByText(label)).toBeNull();
    },
  );

  it.each(["channels", "channel-status", "dreams"])(
    "falls a stale ?panel=%s deep link back to Profile without redirecting",
    (panel) => {
      nav.panel = panel;
      const { container } = render(<SettingsPage />);
      expect(container.querySelector("#panel-profile")).toBeInTheDocument();
      expect(nav.replace).not.toHaveBeenCalled();
    },
  );

  // --- Connections, Workspace and the Model action live here now ---
  it("opens ?panel=connections on the Connections section, not a redirect", () => {
    nav.panel = "connections";
    nav.extra = "connected=Notion";
    render(<SettingsPage />);
    expect(screen.getByTestId("connections-panel")).toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("opens ?panel=workspace on the company name and description", () => {
    nav.panel = "workspace";
    const { container } = render(<SettingsPage />);
    expect(container.querySelector("#panel-workspace")).toBeInTheDocument();
    expect(screen.getByTestId("company-settings")).toBeInTheDocument();
  });

  it("hands ?action=change-provider to the Model panel", () => {
    nav.panel = "providers";
    nav.extra = "action=change-provider";
    render(<SettingsPage />);
    expect(screen.getByTestId("llm-panel")).toHaveAttribute("data-action", "change-provider");
  });

  it("follows a later ?panel= change after a nav click (an in-page link)", () => {
    const { container, rerender } = render(<SettingsPage />);
    fireEvent.click(within(screen.getByRole("navigation", { name: /settings navigation/i })).getByText("Skills"));
    expect(screen.getByTestId("skills-tab")).toBeInTheDocument();
    nav.panel = "connections";
    rerender(<SettingsPage />);
    expect(container.querySelector("#panel-connections")).toBeInTheDocument();
  });

  it("leaves a real tab key alone", () => {
    nav.panel = "skills";
    render(<SettingsPage />);
    expect(nav.replace).not.toHaveBeenCalled();
  });

  // --- Trust boundary: `in` walks the prototype chain, hasOwnProperty doesn't ---
  it("falls back to Profile for a prototype-chain key like ?panel=constructor (review finding 8)", () => {
    nav.panel = "constructor";
    const { container } = render(<SettingsPage />);
    expect(container.querySelector("#panel-profile")).toBeInTheDocument();
    expect(screen.queryByTestId("skills-tab")).not.toBeInTheDocument();
  });
});
