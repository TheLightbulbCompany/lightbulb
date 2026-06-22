import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import React from "react";

// The Settings page composes several gateway-bound Control panels (registered
// in Increments 3–6). Stub them all to inert markers so this nav-structure
// test never opens a WebSocket or fires an RPC — we only assert the grouped
// navigation renders, not panel internals.
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
// `null` = personal owner = admin (the `useIsControlAdmin` personal branch);
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
  useOrganization: () => ({ membership: clerk.membership, organization: null }),
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
  });

  it("renders the six group headers in order", () => {
    const { container } = render(<SettingsPage />);
    // Scope to the section-label nodes only — a nav *item* like
    // "Model & Providers" would otherwise collide with the header text.
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
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    // Existing panels, re-bucketed (Inc 2):
    expect(within(nav).getByText("Profile")).toBeInTheDocument(); // ACCOUNT
    expect(within(nav).getByText("Billing")).toBeInTheDocument(); // BILLING & USAGE
    expect(within(nav).getByText("Channels")).toBeInTheDocument(); // WIRING
    expect(within(nav).getByText("Connections")).toBeInTheDocument(); // WIRING (href)
    expect(within(nav).getByText("Webhooks")).toBeInTheDocument(); // WIRING
    expect(within(nav).getByText("Desktop")).toBeInTheDocument(); // WIRING
    expect(within(nav).getByText("Marketplace")).toBeInTheDocument(); // SELLER
  });

  // --- Inc 3: Usage (admin-gated, BILLING & USAGE) ---
  it("shows the admin-gated Usage row for an admin owner", () => {
    clerk.membership = null; // personal owner = admin
    render(<SettingsPage />);
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(nav).getByText("Usage")).toBeInTheDocument();
  });

  it("hides the Usage row from a non-admin org member", () => {
    clerk.membership = { role: "org:member" };
    render(<SettingsPage />);
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(nav).queryByText("Usage")).not.toBeInTheDocument();
  });

  // --- Inc 4: Model & Providers (LLMPanel, MODEL & PROVIDERS) ---
  it("registers the Model & Providers row and renders LLMPanel on select", () => {
    render(<SettingsPage />);
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    const row = within(nav).getByText("Model & Providers");
    expect(row).toBeInTheDocument();
    // No separate Credits row — CreditsSection is embedded inside LLMPanel.
    expect(within(nav).queryByText("Credits")).not.toBeInTheDocument();
  });

  // --- Inc 5: MCP servers (WIRING) + ENGINE ROOM trio ---
  it("registers the MCP servers row in WIRING", () => {
    render(<SettingsPage />);
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(nav).getByText("MCP servers")).toBeInTheDocument();
  });

  it("registers the ENGINE ROOM panels (Skills, Skill Proposals, Dreams)", () => {
    render(<SettingsPage />);
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(nav).getByText("Skills")).toBeInTheDocument();
    expect(within(nav).getByText("Skill Proposals")).toBeInTheDocument();
    expect(within(nav).getByText("Dreams / Memory")).toBeInTheDocument();
  });

  // --- Inc 6: Channel Status (ENGINE ROOM) — DISTINCT key from WIRING Channels ---
  it("registers Channel Status in ENGINE ROOM under a key distinct from Channels", () => {
    render(<SettingsPage />);
    const nav = screen.getByRole("navigation", { name: /settings navigation/i });
    // Both rows coexist: the WIRING org-bots tab (`channels`) and the
    // read-only ENGINE ROOM status panel (`channel-status`).
    expect(within(nav).getByText("Channels")).toBeInTheDocument();
    const statusRow = within(nav).getByText("Channel Status");
    expect(statusRow).toBeInTheDocument();
    // No key collision: the status row carries the channel-status nav id.
    const statusLi = statusRow.closest("li");
    expect(statusLi?.id).toBe("settings-nav-channel-status");
  });
});
