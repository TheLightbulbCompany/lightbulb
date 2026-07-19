import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useBilling } from "../useBilling";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

describe("useBilling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns null account when /billing/account returns 404 (no row yet)", async () => {
    // Pre-subscribe owners have no billing row; the hook must surface
    // that as the "no account" empty state, not as an error. This is
    // the load-bearing piece of the useApi migration — the hook now
    // catches ApiError(404) where it previously short-circuited at the
    // fetch layer.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(null, { status: 404 }),
    );
    const { result } = renderHook(() => useBilling(), { wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.account).toBeNull();
    expect(result.current.error).toBeUndefined();
    expect(result.current.isSubscribed).toBe(false);
  });

  it("returns the account when /billing/account returns 200", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          is_subscribed: true,
          current_spend: 12.34,
          lifetime_spend: 99.99,
          subscription_status: "active",
          trial_end: null,
          provider_choice: "bedrock_managed",
          byo_provider: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useBilling(), { wrapper });
    await waitFor(() => expect(result.current.account).not.toBeNull());
    expect(result.current.account?.is_subscribed).toBe(true);
    expect(result.current.isSubscribed).toBe(true);
  });

  it("propagates non-404 ApiError as a real error", async () => {
    // Defensive: a 500 must not be silently swallowed into null —
    // the SWR error slot has to surface so callers can render an
    // error state.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "internal" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useBilling(), { wrapper });
    await waitFor(() => expect(result.current.error).toBeDefined());
    expect(result.current.account).toBeUndefined();
  });

  it("startTeamCheckout POSTs /billing/team-checkout and redirects", async () => {
    const orig = window.location;
    // jsdom: make location.href assignable for the assertion.
    Object.defineProperty(window, "location", { value: { href: "" }, writable: true });
    // mockImplementation (not mockResolvedValue) so each call gets its own
    // Response instance — the hook's own SWR mount fetch for /billing/account
    // also hits this mock, and a Response body can only be read once.
    global.fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ checkout_url: "https://checkout/team" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const { result } = renderHook(() => useBilling(), { wrapper });
    await result.current.startTeamCheckout();
    const call = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find((c) =>
      String(c[0]).includes("/billing/team-checkout"),
    );
    expect(call).toBeTruthy();
    expect(window.location.href).toBe("https://checkout/team");
    Object.defineProperty(window, "location", { value: orig, writable: true });
  });

  it("upgradeToUltra POSTs /billing/ultra-checkout and redirects when a checkout_url is returned", async () => {
    // Fresh-checkout path (free-tier / no live sub): the endpoint returns a
    // Checkout URL and the hook must redirect the browser to Stripe.
    const orig = window.location;
    Object.defineProperty(window, "location", { value: { href: "" }, writable: true });
    global.fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ checkout_url: "https://checkout/ultra" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const { result } = renderHook(() => useBilling(), { wrapper });
    await result.current.upgradeToUltra();
    const call = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find((c) =>
      String(c[0]).includes("/billing/ultra-checkout"),
    );
    expect(call).toBeTruthy();
    expect(window.location.href).toBe("https://checkout/ultra");
    Object.defineProperty(window, "location", { value: orig, writable: true });
  });

  it("upgradeToUltra does NOT redirect on the in-place {upgraded:true} swap", async () => {
    // In-place item-swap path (owner already on the flat sub): the endpoint
    // returns {upgraded:true} with no checkout_url, so the hook must refresh
    // the billing row instead of redirecting.
    const orig = window.location;
    Object.defineProperty(window, "location", { value: { href: "" }, writable: true });
    global.fetch = vi.fn().mockImplementation((url: string) => {
      const body = String(url).includes("/billing/ultra-checkout")
        ? { upgraded: true }
        : { is_subscribed: true, plan: "ultra_pro" };
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    const { result } = renderHook(() => useBilling(), { wrapper });
    await result.current.upgradeToUltra();
    const post = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find((c) =>
      String(c[0]).includes("/billing/ultra-checkout"),
    );
    expect(post).toBeTruthy();
    // No redirect on the in-place path — the card flips to the Ultra badge
    // after the refresh.
    expect(window.location.href).toBe("");
    Object.defineProperty(window, "location", { value: orig, writable: true });
  });
});
