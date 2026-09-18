/**
 * H3/M6-R50: channel wiring (`MyChannelsSection`) has no Hermes/AgentCore
 * equivalent. Before this fix, the Settings "Channels" row stayed in the nav
 * and mounting it called `useChannelLinks(true)` unconditionally, waking a
 * parked microVM to render an empty list. This is the defense-in-depth
 * self-gate for a stale `?panel=channels` deep link, on top of the nav row
 * now being hidden via `HIDDEN_ON_AGENTCORE_PANELS`.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { MyChannelsSection } from "../MyChannelsSection";

let gate: "loading" | "hidden" | "shown";
const channelLinksMock = vi.fn();

vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});
vi.mock("@/hooks/useChannelLinks", () => ({
  useChannelLinks: (enabled: boolean) => channelLinksMock(enabled),
}));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ isSubscribed: true }),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ del: vi.fn() }),
}));

describe("MyChannelsSection — AgentCore gating", () => {
  it("renders a runtime notice and holds the RPC once AgentCore is confirmed", () => {
    gate = "hidden";
    channelLinksMock.mockReturnValue({
      channelLinks: undefined,
      error: undefined,
      isLoading: false,
      refresh: vi.fn(),
    });
    render(<MyChannelsSection />);
    expect(screen.getByText("Channels isn't available on this runtime.")).toBeInTheDocument();
    expect(channelLinksMock).toHaveBeenCalledWith(false);
  });

  it("fetches normally on ECS", () => {
    gate = "shown";
    channelLinksMock.mockReturnValue({
      channelLinks: { telegram: [], discord: [], slack: [], can_create_bots: false },
      error: undefined,
      isLoading: false,
      refresh: vi.fn(),
    });
    render(<MyChannelsSection />);
    expect(screen.queryByText("Channels isn't available on this runtime.")).toBeNull();
    expect(channelLinksMock).toHaveBeenCalledWith(true);
  });

  it("holds the RPC while the host isn't known yet", () => {
    gate = "loading";
    channelLinksMock.mockReturnValue({
      channelLinks: undefined,
      error: undefined,
      isLoading: false,
      refresh: vi.fn(),
    });
    render(<MyChannelsSection />);
    expect(channelLinksMock).toHaveBeenCalledWith(false);
  });
});
