import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { ChannelsStatusPanel } from "../ChannelsStatusPanel";

// useGatewayRpc is the only data source. Stub it so each test seeds a
// snapshot and we can assert mutate() is called by the Probe button.
const mockData = vi.fn();
const mockMutate = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({
    data: mockData(),
    error: undefined,
    isLoading: false,
    mutate: mockMutate,
  }),
}));

describe("ChannelsStatusPanel", () => {
  beforeEach(() => {
    mockData.mockReset();
    mockMutate.mockReset();
  });

  it("shows an unavailable state (not 'no channels') when the container is offline", () => {
    // undefined data = container unreachable. This is a different (and more
    // actionable) diagnosis than an empty-but-reachable snapshot, so it must
    // NOT collapse into the "No channels configured" empty state.
    mockData.mockReturnValue(undefined);
    render(<ChannelsStatusPanel />);
    expect(
      screen.getByTestId("channels-status-unavailable"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/no channels configured/i)).toBeNull();
  });

  it("shows an empty state when no channels are configured", () => {
    mockData.mockReturnValue({
      ts: 1,
      channelOrder: [],
      channelLabels: {},
      channels: {},
      channelAccounts: {},
      channelDefaultAccountId: {},
    });
    render(<ChannelsStatusPanel />);
    expect(screen.getByText(/no channels configured/i)).toBeInTheDocument();
  });

  it("renders a heading per channel and a row per account field", () => {
    mockData.mockReturnValue({
      ts: 1,
      channelOrder: ["telegram"],
      channelLabels: { telegram: "Telegram" },
      channels: {},
      channelAccounts: {
        telegram: [
          {
            accountId: "tg-1",
            name: "Main bot",
            configured: true,
            linked: true,
            running: true,
            connected: false,
            mode: "polling",
            lastError: "boom",
          },
        ],
      },
      channelDefaultAccountId: {},
    });
    render(<ChannelsStatusPanel />);
    expect(screen.getByText("Telegram")).toBeInTheDocument();
    expect(screen.getByText("Main bot")).toBeInTheDocument();
    expect(screen.getByText("Configured")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getAllByText("Yes").length).toBeGreaterThan(0);
    expect(screen.getAllByText("No").length).toBeGreaterThan(0);
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("calls mutate() when Probe now is clicked", () => {
    mockData.mockReturnValue({
      ts: 1,
      channelOrder: ["telegram"],
      channelLabels: { telegram: "Telegram" },
      channels: {},
      channelAccounts: { telegram: [{ accountId: "tg-1" }] },
      channelDefaultAccountId: {},
    });
    render(<ChannelsStatusPanel />);
    fireEvent.click(screen.getByRole("button", { name: /probe now/i }));
    expect(mockMutate).toHaveBeenCalledTimes(1);
  });
});
