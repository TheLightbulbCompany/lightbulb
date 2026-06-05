import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ControlSidebar } from "../ControlSidebar";

// SWR mock: each test seeds a different /users/me response.
const mockSWRData = vi.fn();
vi.mock("swr", () => ({
  default: () => ({ data: mockSWRData(), error: null, isLoading: false, mutate: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: vi.fn(),
    post: vi.fn(),
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ membership: null }),
}));

describe("ControlSidebar", () => {
  beforeEach(() => {
    mockSWRData.mockReset();
  });

  it("shows the Credits item for bedrock_claude users", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.getByText("Credits")).toBeInTheDocument();
  });

  it("hides the Credits item for byo_key users", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key" });
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.queryByText("Credits")).not.toBeInTheDocument();
  });

  it("hides the Credits item for chatgpt_oauth users", () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.queryByText("Credits")).not.toBeInTheDocument();
  });

  it("shows the Credits item while /users/me is still loading (undefined)", () => {
    mockSWRData.mockReturnValue(undefined);
    render(<ControlSidebar activePanel="overview" />);
    // Loading state: render the item rather than flash it on resolve.
    expect(screen.getByText("Credits")).toBeInTheDocument();
  });

  it("shows the Channels nav item for every provider", () => {
    // Channels is provider-agnostic — unlike Credits, it isn't gated on
    // provider_choice, so any seeded value (here byo_key) must still show it.
    mockSWRData.mockReturnValue({ provider_choice: "byo_key" });
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.getByText("Channels")).toBeInTheDocument();
  });
});
