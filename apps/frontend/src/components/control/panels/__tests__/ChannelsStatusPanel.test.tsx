import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { ChannelsStatusPanel } from "../ChannelsStatusPanel";

// useGatewayRpc is the only data source. Stub it so each test seeds a
// snapshot and we can assert mutate() is called by the Probe button.
const mockData = vi.fn();
const mockMutate = vi.fn();
const mockPairingData = vi.fn();
const mockPairingRefresh = vi.fn();
const mockApiPost = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({
    data: mockData(),
    error: undefined,
    isLoading: false,
    mutate: mockMutate,
  }),
}));
vi.mock("@/hooks/useChannelPairingRequests", () => ({
  useChannelPairingRequests: () => ({
    data: mockPairingData(),
    error: undefined,
    isLoading: false,
    refresh: mockPairingRefresh,
  }),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ post: mockApiPost }),
}));

describe("ChannelsStatusPanel", () => {
  beforeEach(() => {
    mockData.mockReset();
    mockMutate.mockReset();
    mockPairingData.mockReset();
    mockPairingData.mockReturnValue({ requests: [] });
    mockPairingRefresh.mockReset();
    mockApiPost.mockReset();
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

  it("surfaces immutable Slack identity and approves without a pairing code", async () => {
    mockData.mockReturnValue({
      ts: 1,
      channelOrder: ["slack"],
      channelLabels: { slack: "Slack" },
      channels: {},
      channelAccounts: { slack: [{ accountId: "friday" }] },
      channelDefaultAccountId: {},
    });
    mockPairingData.mockReturnValue({
      requests: [
        {
          requestId: "request-opaque-1",
          channel: "slack",
          channelLabel: "Slack",
          accountId: "friday",
          accountLabel: "Friday",
          senderId: "U_IMMUTABLE",
          senderLabel: "Aden Kim",
          createdAt: "2026-09-04T23:00:00Z",
          lastSeenAt: "2026-09-04T23:00:00Z",
          expiresAt: "2026-09-05T00:00:00Z",
          notifySupported: true,
        },
      ],
    });
    mockApiPost.mockResolvedValue({ notification: "sent" });
    mockPairingRefresh.mockResolvedValue(undefined);

    render(<ChannelsStatusPanel />);
    expect(screen.getByText("Pending Slack access")).toBeInTheDocument();
    expect(screen.getByText("Aden Kim")).toBeInTheDocument();
    expect(screen.getByText("U_IMMUTABLE")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /approve/i }));

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith("/channels/pairing/approve", {
        channel: "slack",
        account_id: "friday",
        request_id: "request-opaque-1",
      });
    });
    expect(JSON.stringify(mockApiPost.mock.calls)).not.toMatch(/code/i);
    expect(mockPairingRefresh).toHaveBeenCalledTimes(1);
  });
});
