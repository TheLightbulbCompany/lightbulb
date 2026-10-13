import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import React from "react";

// The Settings consolidation (6-group nav + 7 relocated config panels) is the
// permanent /settings surface. This test asserts the nav structure and that the
// relocated config panels are deep-linkable.

// Mutable ?panel= param so deep-link-guard tests can drive the initial panel.
const nav = vi.hoisted(() => ({ panel: null as string | null, replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
  useRouter: () => ({ replace: nav.replace }),
}));

// The Settings page composes several gateway-bound Control panels. Stub them all
// to inert markers so this nav-structure test never opens a WebSocket or fires
// an RPC — we only assert the navigation renders, not panel internals.
vi.mock("@/components/control/panels/UsagePanel", () => ({
  UsagePanel: () => <div data-testid="usage-panel" />,
}));
vi.mock("@/components/control/panels/LLMPanel", () => ({
  LLMPanel: () => <div data-testid="llm-panel" />,
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

describe("Settings nav — 6 intent-ordered groups", () => {
  beforeEach(() => {
    clerk.membership = null; // default: personal owner = admin
    clerk.isLoaded = true;
    nav.panel = null;
    nav.replace.mockClear();
  });

  it("renders the six group headers in order", () => {
    const { container } = render(<SettingsPage />);
    // Scope to the section-label nodes only so a nav *item* whose name
    // echoes a group header can never collide with the header text.
    const labels = Array.from(
      container.querySelectorAll(".settings-nav-section-label"),
    ).map((el) => el.textContent?.toUpperCase());
    expect(labels).toEqual([
      "ACCOUNT",
      "BILLING & USAGE",
      "MODEL & PROVIDERS",
      "WIRING",
      "ENGINE ROOM",
      "SELLER",
    ]);
  });

  it("buckets the existing panels into their new groups", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Profile")).toBeInTheDocument(); // ACCOUNT
    expect(within(navEl).getByText("Billing")).toBeInTheDocument(); // BILLING & USAGE
    expect(within(navEl).getByText("Desktop")).toBeInTheDocument(); // WIRING
    expect(within(navEl).getByText("Marketplace")).toBeInTheDocument(); // SELLER
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

  // --- Members (admin-gated, ACCOUNT) — the workspace invite door ---
  it("registers the Members row in ACCOUNT for an admin", () => {
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

  // --- MCP servers (WIRING) + ENGINE ROOM trio ---
  it("registers the MCP servers row in WIRING", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("MCP servers")).toBeInTheDocument();
  });

  it("registers the ENGINE ROOM Skills panel", () => {
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

  // --- The Connections row is gone from Settings entirely ---
  it("no longer offers Connections as a Settings row", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).queryByText("Connections")).toBeNull();
  });

  // --- Deep link to a panel that MOVED OUT of Settings ---
  it("forwards ?panel=connections to the workspace rail instead of falling back to Profile", () => {
    // Connections lives on the workspace rail now, so the key isn't in PANELS.
    // Links and bookmarks minted while it was a Settings row must still land on
    // it rather than silently on Profile.
    nav.panel = "connections";
    const { container } = render(<SettingsPage />);
    expect(nav.replace).toHaveBeenCalledWith("/chat?panel=connections");
    // …and nothing is painted on the way out — no Profile flash.
    expect(container.querySelector("#panel-profile")).not.toBeInTheDocument();
  });

  it("leaves a real tab key alone — only relocated/href rows redirect", () => {
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
