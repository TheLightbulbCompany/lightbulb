import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

import { useHire } from "../useHire";
import { ApiError } from "@/lib/api";
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

describe("useHire — pending automations", () => {
  beforeEach(() => {
    mockDeploy.mockReset();
    mockRefreshAgents.mockReset();
    mockShowInstallToast.mockReset();
    mockShowErrorToast.mockReset();
    mockMarkInstalled.mockReset();
    mockClose.mockReset();
    mockRefreshAgents.mockResolvedValue(undefined);
  });

  it("counts the webhook automations the deploy left for the buyer to wire", async () => {
    // The deploy deliberately arms nothing for webhook triggers (each binds a
    // resource only the buyer can name). Without this count the hired agent
    // arrives silently incomplete — the whole point of shipping automations.
    mockDeploy.mockResolvedValue(
      deployResult({
        automations_pending: [
          { provider: "github", event_id: "push", needs_resource: true },
          { provider: "stripe", event_id: "invoice.payment_failed", needs_resource: false },
        ],
      }),
    );
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing());
    });
    await waitFor(() =>
      expect(mockShowInstallToast).toHaveBeenCalledWith(
        expect.objectContaining({ automationsPending: 2 }),
      ),
    );
  });

  it("passes no count when the listing ships no webhook automations", async () => {
    mockDeploy.mockResolvedValue(deployResult());
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing());
    });
    await waitFor(() =>
      expect(mockShowInstallToast).toHaveBeenCalledWith(
        expect.objectContaining({ automationsPending: undefined }),
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
  it("tells the buyer it is still installing on a 409 deploy_in_progress, not to retry", async () => {
    // The backend fenced an OVERLAPPING second click; the first deploy is still
    // running and WILL install the agent. "try again" is the advice that
    // produced a duplicate agent on prod.
    mockDeploy.mockRejectedValue(
      new ApiError(409, { detail: { error: "deploy_in_progress" } }, "conflict"),
    );
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing({ name: "Atlas" }));
    });
    expect(mockShowErrorToast).toHaveBeenCalledWith(
      expect.stringContaining("already installing"),
    );
    expect(mockShowErrorToast).not.toHaveBeenCalledWith(
      expect.stringContaining("try again"),
    );
  });

  it("keeps the retry message for a 409 that is NOT a structured shape it knows", async () => {
    // e.g. Requires Ultra — unknown 409s must not be relabelled as
    // "installing" or auto-retried.
    mockDeploy.mockRejectedValue(
      new ApiError(409, { detail: { error: "requires_ultra" } }, "conflict"),
    );
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing({ name: "Atlas" }));
    });
    expect(mockShowErrorToast).toHaveBeenCalledWith(expect.stringContaining("try again"));
  });

  it("names the missing providers on a 409 missing_credentials, and does NOT say 'try again'", async () => {
    // Retrying can never succeed until the buyer connects the provider — the
    // backend sends the list specifically so the UI can say which.
    mockDeploy.mockRejectedValue(
      new ApiError(
        409,
        { detail: { error: "missing_credentials", missing: ["google", "slack"] } },
        "conflict",
      ),
    );
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire(listing({ name: "Atlas" }));
    });
    expect(mockShowErrorToast).toHaveBeenCalledWith(expect.stringContaining("google, slack"));
    expect(mockShowErrorToast).not.toHaveBeenCalledWith(expect.stringContaining("try again"));
  });

  describe("409 deploy_busy — a DIFFERENT listing of the buyer's is installing", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("tells the buyer their container is busy, and does NOT say 'try again'", async () => {
      mockDeploy.mockRejectedValue(
        new ApiError(409, { detail: { error: "deploy_busy", retry_after_s: 20 } }, "conflict"),
      );
      const { result } = renderHook(() => useHire());
      await act(async () => {
        await result.current.hire(listing({ name: "Atlas" }));
      });
      // sticky: the notice must outlive the toast's 5.5s auto-dismiss — the
      // pending auto-retry fires ~retry_after_s later.
      expect(mockShowErrorToast).toHaveBeenCalledWith(
        expect.stringContaining("busy"),
        expect.objectContaining({ sticky: true }),
      );
      expect(mockShowErrorToast).not.toHaveBeenCalledWith(expect.stringContaining("try again"));
    });

    it("auto-retries once after retry_after_s, and the retry can succeed", async () => {
      // First call: owner-busy refusal. Second call (the auto-retry): succeeds.
      mockDeploy
        .mockRejectedValueOnce(
          new ApiError(409, { detail: { error: "deploy_busy", retry_after_s: 20 } }, "conflict"),
        )
        .mockResolvedValueOnce(deployResult());

      const { result } = renderHook(() => useHire());
      await act(async () => {
        await result.current.hire(listing({ name: "Atlas", slug: "atlas" }));
      });
      expect(mockDeploy).toHaveBeenCalledTimes(1);
      expect(mockShowInstallToast).not.toHaveBeenCalled();

      // Advance past the server-supplied retry_after_s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });

      expect(mockDeploy).toHaveBeenCalledTimes(2);
      // Already inside `act` above, which flushes the retried hire()'s state
      // updates — no `waitFor` needed (and `waitFor`'s own internal polling
      // timer doesn't advance under fake timers, which just hangs the test).
      expect(mockShowInstallToast).toHaveBeenCalled();
    });

    it("does not retry a second time — a repeat deploy_busy falls through to the generic message", async () => {
      mockDeploy.mockRejectedValue(
        new ApiError(409, { detail: { error: "deploy_busy", retry_after_s: 20 } }, "conflict"),
      );
      const { result } = renderHook(() => useHire());
      await act(async () => {
        await result.current.hire(listing({ name: "Atlas" }));
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });

      expect(mockDeploy).toHaveBeenCalledTimes(2);
      // No THIRD call was scheduled off the retry's own failure.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(mockDeploy).toHaveBeenCalledTimes(2);
      expect(mockShowErrorToast).toHaveBeenLastCalledWith(expect.stringContaining("try again"));
    });
  });
});
