import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import React from "react";

/**
 * M6 task 10-D, rows 18/24. Dreams / Memory and Channel Status have no
 * Hermes/AgentCore equivalent — they drop off the Settings nav on a confirmed
 * AgentCore workspace and stay while the host isn't known yet.
 */

const nav = vi.hoisted(() => ({ panel: null as string | null, replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.panel ? { panel: nav.panel } : {}),
  useRouter: () => ({ replace: nav.replace }),
}));

let gate: "loading" | "hidden" | "shown";
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});

vi.mock("@/components/control/panels/UsagePanel", () => ({ UsagePanel: () => <div /> }));
vi.mock("@/components/control/panels/LLMPanel", () => ({ LLMPanel: () => <div /> }));
vi.mock("@/components/control/panels/IntegrationsPanel", () => ({ IntegrationsPanel: () => <div /> }));
vi.mock("@/components/control/panels/SkillsTab", () => ({ SkillsTab: () => <div /> }));
vi.mock("@/components/control/panels/SkillProposalsPanel", () => ({ SkillProposalsPanel: () => <div /> }));
vi.mock("@/components/control/panels/ChannelsStatusPanel", () => ({
  ChannelsStatusPanel: () => <div data-testid="channels-status-panel" />,
}));
vi.mock("@/components/control/panels/DreamsPanel", () => ({
  DreamsPanel: () => <div data-testid="dreams-panel" />,
}));

vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { fullName: "Test User", firstName: "Test", lastName: "User", primaryEmailAddress: { emailAddress: "t@t.co" } },
  }),
  useAuth: () => ({ getToken: () => Promise.resolve("jwt"), isLoaded: true }),
  useOrganization: () => ({ membership: null, organization: null, isLoaded: true }),
  useClerk: () => ({ signOut: vi.fn() }),
  UserButton: () => <div data-testid="user-button" />,
}));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: null, isLoading: false, error: null, isSubscribed: false, openPortal: vi.fn() }),
}));

import SettingsPage from "../page";

describe("Settings nav — Dreams/Channel Status on AgentCore", () => {
  beforeEach(() => {
    nav.panel = null;
    nav.replace.mockClear();
    gate = "hidden";
  });

  it("drops Dreams / Memory and Channel Status once AgentCore is confirmed", () => {
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).queryByText("Dreams / Memory")).toBeNull();
    expect(within(navEl).queryByText("Channel Status")).toBeNull();
    // WIRING's Channels row is a different key — untouched by this hide.
    expect(within(navEl).getByText("Channels")).toBeInTheDocument();
  });

  it("still lists both rows while the host isn't known yet", () => {
    gate = "loading";
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Dreams / Memory")).toBeInTheDocument();
    expect(within(navEl).getByText("Channel Status")).toBeInTheDocument();
  });

  it("lists both rows on ECS", () => {
    gate = "shown";
    render(<SettingsPage />);
    const navEl = screen.getByRole("navigation", { name: /settings navigation/i });
    expect(within(navEl).getByText("Dreams / Memory")).toBeInTheDocument();
    expect(within(navEl).getByText("Channel Status")).toBeInTheDocument();
  });

  it("a stale ?panel=channel-status deep link still resolves to the panel (which self-gates)", () => {
    // The nav hides the door, but a bookmarked/persisted link must not 404 or
    // silently fall back to Profile — the panel component owns its own notice.
    nav.panel = "channel-status";
    const { container } = render(<SettingsPage />);
    expect(screen.getByTestId("channels-status-panel")).toBeInTheDocument();
    expect(container.querySelector("#panel-profile")).not.toBeInTheDocument();
  });
});
