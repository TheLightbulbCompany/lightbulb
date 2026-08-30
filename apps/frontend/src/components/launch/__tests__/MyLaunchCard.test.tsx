import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi, beforeEach } from "vitest";

const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post, put: vi.fn(), patch, del: vi.fn() }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../JoinForm", () => ({ JoinForm: () => <div data-testid="join-form" /> }));
vi.mock("../ConnectStripeForm", () => ({ ConnectStripeForm: () => <div data-testid="connect-stripe-form" /> }));
vi.mock("../LaunchStatusLines", () => ({ LaunchStatusLines: () => <div data-testid="status-lines" /> }));
const useLaunchRef = vi.fn<() => string | null>(() => null);
vi.mock("../useLaunchRef", () => ({ useLaunchRef: () => useLaunchRef() }));

import { LAUNCH_REF_KEY, type LaunchInfo, type LaunchMe } from "@/lib/launch";
import { MyLaunchCard } from "../MyLaunchCard";

const info: LaunchInfo = { phase: "live", drop_at: "2026-09-18T16:00:00+00:00", competition_start_at: "", competition_end_at: "", pot_cents: 0, signups: 0, competing: 0, on_board: 0, comp_slots: 220, pot_per_entrant_cents: 1000 };
const me = (over: Partial<LaunchMe>): LaunchMe => ({
  phase: "live",
  waitlist: { position: 47, effective_position: 17, moved_up: 30, referral_jump: 10, referral_code: "P9AB3CDE", in_comp_window: true, referral_link: "https://flickmeon.ai/?ref=P9AB3CDE", referrals_signed_up: 3, referrals_paid: 1, free_months_banked: 1 },
  activated: true, activated_at: "2026-09-19T00:00:00Z", entry: null, joined: false, has_key: false, can_join: true, reason: null, idea: null, ...over,
});

function mount(refCode: string | null = null, i: LaunchInfo = info) {
  useLaunchRef.mockReturnValue(refCode);
  return render(<SWRConfig value={{ provider: () => new Map() }}><MyLaunchCard info={i} /></SWRConfig>);
}

describe("MyLaunchCard", () => {
  beforeEach(() => { get.mockReset(); post.mockReset(); patch.mockReset(); useLaunchRef.mockReturnValue(null); localStorage.clear(); });

  it("pre-drop: shows the shared status lines, not a form", async () => {
    get.mockResolvedValue(me({ phase: "waitlist", activated: false, can_join: false, reason: "phase_waitlist" }));
    mount(null, { ...info, phase: "waitlist" });
    expect(await screen.findByTestId("status-lines")).toBeInTheDocument();
    expect(screen.queryByTestId("join-form")).not.toBeInTheDocument();
  });

  it("post-drop, not activated: prompts to finish onboarding", async () => {
    get.mockResolvedValue(me({ activated: false, can_join: false, reason: "not_activated" }));
    mount();
    expect(await screen.findByText(/finish onboarding/i)).toBeInTheDocument();
    expect(screen.queryByTestId("join-form")).not.toBeInTheDocument();
  });

  it("activated, not joined, can_join: renders JoinForm", async () => {
    get.mockResolvedValue(me({}));
    mount();
    expect(await screen.findByTestId("join-form")).toBeInTheDocument();
  });

  it("activated, not joined, blocked: renders the reason copy instead of JoinForm", async () => {
    get.mockResolvedValue(me({ can_join: false, reason: "no_subscription" }));
    mount();
    expect(await screen.findByText(/Redeem your code or subscribe/)).toBeInTheDocument();
    expect(screen.queryByTestId("join-form")).not.toBeInTheDocument();
  });

  it("joined, no key: prompts to connect Stripe", async () => {
    get.mockResolvedValue(me({ joined: true, has_key: false, entry: { display_name: "Ada", public: true, entered_at: "t", has_key: false, currency: null, baseline_cents: 0, window_cents: 0, delta_cents: 0, refreshed_at: null, refresh_error: null } }));
    mount();
    expect(await screen.findByText(/Connect a read-only Stripe key/)).toBeInTheDocument();
    expect(screen.getByTestId("connect-stripe-form")).toBeInTheDocument();
  });

  it("joined, on the board: shows the lift line, no form", async () => {
    get.mockResolvedValue(me({ joined: true, has_key: true, entry: { display_name: "Ada", public: true, entered_at: "t", has_key: true, currency: "usd", baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: "t", refresh_error: null } }));
    mount();
    expect(await screen.findByText(/You're in as Ada/)).toBeInTheDocument();
    expect(screen.getByText("$50")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-stripe-form")).not.toBeInTheDocument();
  });

  it("refresh_error: shows the error and ConnectStripeForm again", async () => {
    get.mockResolvedValue(me({ joined: true, has_key: true, entry: { display_name: "Ada", public: true, entered_at: "t", has_key: true, currency: "usd", baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: "t", refresh_error: "stripe_auth" } }));
    mount();
    expect(await screen.findByText(/stripe_auth/)).toBeInTheDocument();
    expect(screen.getByTestId("connect-stripe-form")).toBeInTheDocument();
  });

  it("the public checkbox PATCHes /launch/entry on change", async () => {
    get.mockResolvedValue(me({ joined: true, has_key: true, entry: { display_name: "Ada", public: true, entered_at: "t", has_key: true, currency: "usd", baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: "t", refresh_error: null } }));
    patch.mockResolvedValue({});
    mount();
    const checkbox = await screen.findByLabelText(/show my name on the board/i);
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/launch/entry", { public: false }));
  });

  it("a failed PATCH reports an error instead of failing silently", async () => {
    get.mockResolvedValue(me({ joined: true, has_key: true, entry: { display_name: "Ada", public: true, entered_at: "t", has_key: true, currency: "usd", baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: "t", refresh_error: null } }));
    patch.mockRejectedValue(new Error("network blip"));
    mount();
    const checkbox = await screen.findByLabelText(/show my name on the board/i);
    fireEvent.click(checkbox);
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/launch/entry", { public: false }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't update/i);
  });

  it("attributes a stored referral once and clears it on a resolved response", async () => {
    get.mockResolvedValue(me({}));
    post.mockResolvedValue({ attributed: true });
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    mount("K7QX2MPP");
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    await waitFor(() => expect(localStorage.getItem(LAUNCH_REF_KEY)).toBeNull());
  });

  it("does NOT clear the stored referral when the request throws", async () => {
    get.mockResolvedValue(me({}));
    post.mockRejectedValue(new Error("network blip"));
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    mount("K7QX2MPP");
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("K7QX2MPP");
  });
});
