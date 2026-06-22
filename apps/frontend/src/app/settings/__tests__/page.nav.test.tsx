import { describe, it, expect, vi } from "vitest";
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

// Personal owner (no org membership) → treated as admin, so admin-gated nav
// rows (Usage) render. `useOrganization` returning `{ membership: null }`
// mirrors the personal-owner branch of `useIsControlAdmin`.
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
  useOrganization: () => ({ membership: null, organization: null }),
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
  it("renders the six group headers in order", () => {
    render(<SettingsPage />);
    const labels = screen
      .getAllByText(
        /^(ACCOUNT|BILLING & USAGE|MODEL & PROVIDERS|WIRING|ENGINE ROOM|SELLER)$/i,
      )
      .map((el) => el.textContent?.toUpperCase());
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
});
