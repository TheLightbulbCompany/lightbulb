import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

import { useHire } from "../useHire";
import type { Listing } from "@/lib/marketplace/types";
import type { DeployResult } from "@/hooks/useCatalog";

// useHire fans out to useCatalog().deploy + useAgents().refresh + the drawer's
// showInstallToast. Mock all three so we can assert the toast's acpNote mapping
// in isolation.
const mockDeploy = vi.fn();
vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({ deploy: mockDeploy }),
}));

const mockRefreshAgents = vi.fn();
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ refresh: mockRefreshAgents }),
}));

const mockShowInstallToast = vi.fn();
const mockShowErrorToast = vi.fn();
const mockMarkInstalled = vi.fn();
const mockClose = vi.fn();
vi.mock("../MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    close: mockClose,
    showInstallToast: mockShowInstallToast,
    showErrorToast: mockShowErrorToast,
    setFreshlyInstalled: vi.fn(),
    markInstalled: mockMarkInstalled,
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("test-token") }),
  // Buyer is also the seller so the free/self-deploy path runs and we avoid
  // the Stripe-checkout branch entirely (price_cents stays 0 anyway).
  useUser: () => ({ user: { id: "u_buyer", primaryEmailAddress: null } }),
}));

function deployResult(overrides: Partial<DeployResult> = {}): DeployResult {
  return {
    slug: "atlas",
    version: 1,
    agent_id: "agent_atlas",
    name: "Atlas",
    skills_added: [],
    plugins_enabled: [],
    ...overrides,
  };
}

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    listing_id: "lst_atlas",
    slug: "atlas",
    name: "Atlas",
    description_md: "",
    format: "openclaw",
    price_cents: 0,
    tags: [],
    seller_id: "u_seller",
    status: "published",
    version: 1,
    published_at: null,
    created_at: "2026-05-10T12:00:00Z",
    ...overrides,
  };
}

describe("useHire — ACP note mapping", () => {
  beforeEach(() => {
    mockDeploy.mockReset();
    mockRefreshAgents.mockReset();
    mockShowInstallToast.mockReset();
    mockShowErrorToast.mockReset();
    mockMarkInstalled.mockReset();
    mockClose.mockReset();
    mockRefreshAgents.mockResolvedValue(undefined);
  });

  it("passes acpNote='enabled' when deploy returns acp_requested && acp_enabled", async () => {
    mockDeploy.mockResolvedValue(deployResult({ acp_requested: true, acp_enabled: true }));
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing());
    });
    await waitFor(() =>
      expect(mockShowInstallToast).toHaveBeenCalledWith(
        expect.objectContaining({ acpNote: "enabled" }),
      ),
    );
  });

  it("passes acpNote='unavailable' when requested but not enabled", async () => {
    mockDeploy.mockResolvedValue(deployResult({ acp_requested: true, acp_enabled: false }));
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing());
    });
    await waitFor(() =>
      expect(mockShowInstallToast).toHaveBeenCalledWith(
        expect.objectContaining({ acpNote: "unavailable" }),
      ),
    );
  });

  it("passes no acpNote when ACP was not requested", async () => {
    mockDeploy.mockResolvedValue(deployResult({ acp_requested: false }));
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing());
    });
    await waitFor(() =>
      expect(mockShowInstallToast).toHaveBeenCalledWith(
        expect.objectContaining({ acpNote: undefined }),
      ),
    );
  });
});

describe("useHire — reliability", () => {
  beforeEach(() => {
    mockDeploy.mockReset();
    mockRefreshAgents.mockReset();
    mockShowInstallToast.mockReset();
    mockShowErrorToast.mockReset();
    mockMarkInstalled.mockReset();
    mockClose.mockReset();
    mockRefreshAgents.mockResolvedValue(undefined);
  });

  it("marks the listing slug installed on a successful hire (stops re-click duplicates)", async () => {
    mockDeploy.mockResolvedValue(deployResult());
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing({ slug: "atlas" }));
    });
    await waitFor(() => expect(mockMarkInstalled).toHaveBeenCalledWith("atlas"));
  });

  it("surfaces an error toast and does NOT close the drawer when deploy rejects", async () => {
    mockDeploy.mockRejectedValue(new Error("500 Internal Server Error"));
    const { result } = renderHook(() => useHire());

    // The hook swallows the rejection (no unhandled promise) — the drawer
    // used to freeze mid-spinner instead.
    await act(async () => {
      await result.current.hire(listing({ name: "Atlas" }));
    });

    expect(mockShowErrorToast).toHaveBeenCalledWith(expect.stringContaining("Atlas"));
    expect(mockShowInstallToast).not.toHaveBeenCalled();
    expect(mockClose).not.toHaveBeenCalled();
    // isHiring resets so the buyer can retry.
    expect(result.current.isHiring).toBe(false);
  });
});
