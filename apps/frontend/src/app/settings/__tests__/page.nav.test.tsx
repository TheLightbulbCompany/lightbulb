import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import React from "react";

// The Settings consolidation (6-group nav + 7 relocated config panels) is the
// permanent /settings surface. This test asserts the nav structure and that the
// relocated config panels are deep-linkable.

// Mutable ?panel= param so deep-link-guard tests can drive the initial panel.
const nav = vi.hoisted(() => ({ panel: null as string | null }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
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
vi.mock("@/components/control/panels/SkillProposalsPanel", () => ({
  SkillProposalsPanel: () => <div data-testid="skill-proposals-panel" />,
}));
vi.mock("@/components/control/panels/DreamsPanel", () => ({
  DreamsPanel: () => <div data-testid="dreams-panel" />,
}));
vi.mock("@/components/control/panels/ChannelsStatusPanel", () => ({
  ChannelsStatusPanel: () => <div data-testid="channels-status-panel" />,
}));

// Mutable membership holder so individual tests can flip admin ↔ non-admin.
// `null` = personal owner = admin (the `useIsWorkspaceAdmin` personal branch);
// `{ role: "org:member" }` = a non-admin org member (Usage must hide).
const clerk = vi.hoisted(() => ({
  membership: null as { role: string } | null,
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
  useOrganization: () => ({ membership: clerk.membership, organization: null, isLoaded: true }),
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
    nav.panel = null;
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
    expect(within(navEl).getByText("Channels")).toBeInTheDocument(); // WIRING
    expect(within(navEl).getByText("Connections")).toBeInTheDocument(); // WIRING (href)
    expect(within(navEl).getByText("Webhooks")).toBeInTheDocument(); // WIRING
    expect(within(navEl).getByText("Desktop")).toBeInTheDocument(); // WIRING
    expect(within(navEl).getByText("Marketplace")).toBeInTheDocument(); // SELLER
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

  it("registers the ENGINE ROOM panels (Skills, Skill Proposals, Dreams)", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Skills")).toBeInTheDocument();
    expect(within(navEl).getByText("Skill Proposals")).toBeInTheDocument();
    expect(within(navEl).getByText("Dreams / Memory")).toBeInTheDocument();
  });

  // --- Channel Status (ENGINE ROOM) — DISTINCT key from WIRING Channels ---
  it("registers Channel Status in ENGINE ROOM under a key distinct from Channels", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    // Both rows coexist: the WIRING org-bots tab (`channels`) and the
    // read-only ENGINE ROOM status panel (`channel-status`).
    expect(within(navEl).getByText("Channels")).toBeInTheDocument();
    const statusRow = within(navEl).getByText("Channel Status");
    expect(statusRow).toBeInTheDocument();
    // No key collision: the status row carries the channel-status nav id.
    const statusLi = statusRow.closest("li");
    expect(statusLi?.id).toBe("settings-nav-channel-status");
  });

  // --- Deep link: a relocated-panel ?panel= key resolves ---
  it("honors a ?panel=channel-status deep link", () => {
    nav.panel = "channel-status";
    render(<SettingsPage />);
    // The channel-status panel mounts (its stub marker is present).
    expect(screen.getByTestId("channels-status-panel")).toBeInTheDocument();
  });
});
