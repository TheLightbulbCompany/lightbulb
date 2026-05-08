import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

const deployFn = vi.fn();
const checkoutFn = vi.fn();
const refreshAgents = vi.fn();

vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({ deploy: deployFn }),
}));
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ refresh: refreshAgents }),
}));
vi.mock("@/lib/marketplace/api", () => ({
  checkout: (opts: unknown) => checkoutFn(opts),
}));

const showInstallToast = vi.fn();
const close = vi.fn();
const setFreshlyInstalled = vi.fn();

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ showInstallToast, close, setFreshlyInstalled }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_abc") }),
  useUser: () => ({ user: { primaryEmailAddress: { emailAddress: "buyer@example.com" } } }),
}));

vi.stubGlobal("location", { href: "http://localhost:3000/chat", origin: "http://localhost:3000" });

import { useHire } from "@/components/marketplace/drawer/useHire";

describe("useHire", () => {
  beforeEach(() => {
    deployFn.mockReset();
    checkoutFn.mockReset();
    refreshAgents.mockReset();
    showInstallToast.mockReset();
    close.mockReset();
    setFreshlyInstalled.mockReset();
  });

  it("free listing: calls deploy, shows toast, closes drawer", async () => {
    // useCatalog().deploy returns DeployResult (slug, version, agent_id, name, …)
    deployFn.mockResolvedValueOnce({
      slug: "atlas",
      version: 1,
      agent_id: "ag_42",
      name: "Atlas",
      skills_added: [],
      plugins_enabled: [],
    });
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire({
        listing_id: "l1",
        slug: "atlas",
        name: "Atlas",
        price_cents: 0,
      } as unknown as Parameters<typeof result.current.hire>[0]);
    });
    expect(deployFn).toHaveBeenCalledWith("atlas");
    expect(refreshAgents).toHaveBeenCalled();
    expect(setFreshlyInstalled).toHaveBeenCalledWith("ag_42");
    expect(showInstallToast).toHaveBeenCalledWith({ agentName: "Atlas", agentId: "ag_42" });
    await waitFor(() => expect(close).toHaveBeenCalled(), { timeout: 1500 });
  });

  it("paid listing: redirects to Stripe checkout via window.location", async () => {
    checkoutFn.mockResolvedValueOnce({
      checkout_url: "https://checkout.stripe.com/abc",
      session_id: "cs_1",
    });
    const { result } = renderHook(() => useHire());
    await act(async () => {
      await result.current.hire({
        listing_id: "l2",
        slug: "pulse",
        name: "Pulse",
        price_cents: 1200,
      } as unknown as Parameters<typeof result.current.hire>[0]);
    });
    expect(checkoutFn).toHaveBeenCalledWith(
      expect.objectContaining({
        listingSlug: "pulse",
        jwt: "tok_abc",
        email: "buyer@example.com",
      }),
    );
    expect(window.location.href).toBe("https://checkout.stripe.com/abc");
  });
});
