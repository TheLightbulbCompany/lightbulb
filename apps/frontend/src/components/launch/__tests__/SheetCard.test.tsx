import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi, beforeEach } from "vitest";

const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post, put: vi.fn(), patch, del: vi.fn() }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../JoinForm", () => ({ JoinForm: () => <div data-testid="join-form" /> }));
vi.mock("../ConnectStripeForm", () => ({
  ConnectStripeForm: ({ onConnected }: { onConnected: (e: unknown) => void }) => (
    <button
      type="button"
      data-testid="connect-stripe-form"
      onClick={() =>
        onConnected({
          display_name: "Ada", public: true, entered_at: "t", has_key: true, currency: "usd",
          baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: null, refresh_error: null,
        })
      }
    />
  ),
}));
const useLaunchRef = vi.fn<() => string | null>(() => null);
vi.mock("../useLaunchRef", () => ({ useLaunchRef: () => useLaunchRef() }));

import { LAUNCH_REF_KEY, type LaunchInfo, type LaunchMe } from "@/lib/launch";
import { SheetCard } from "../SheetCard";

const info = (over: Partial<LaunchInfo> = {}): LaunchInfo => ({
  phase: "open",
  sheet_closes_at: "2026-09-30T16:00:00+00:00",
  competition_start_at: "2026-10-01T16:00:00+00:00",
  competition_end_at: "2026-12-01T16:00:00+00:00",
  signups: 40,
  comp_slots: 200,
  referral_jump: 10,
  ...over,
});

const me = (over: Partial<LaunchMe> = {}): LaunchMe => ({
  phase: "open",
  joined: false,
  position: null,
  effective_position: null,
  referrals_subscribed: 0,
  referral_code: null,
  referral_link: null,
  credits_milestone_reached: 0,
  winner: false,
  compete_eligible: false,
  onboarded: true,
  entry: null,
  ...over,
});

const joined = (over: Partial<LaunchMe> = {}): LaunchMe =>
  me({
    joined: true,
    position: 47,
    effective_position: 17,
    referrals_subscribed: 3,
    referral_code: "P9AB3CDE",
    referral_link: "https://flickmeon.ai/?ref=P9AB3CDE",
    compete_eligible: true,
    ...over,
  });

function mount(i: LaunchInfo = info()) {
  return render(<SWRConfig value={{ provider: () => new Map() }}><SheetCard info={i} /></SWRConfig>);
}

describe("SheetCard", () => {
  beforeEach(() => { get.mockReset(); post.mockReset(); patch.mockReset(); useLaunchRef.mockReturnValue(null); localStorage.clear(); });

  it("not onboarded: Onboard-first CTA into /chat, no join form", async () => {
    get.mockResolvedValue(me({ onboarded: false }));
    mount();
    expect(await screen.findByRole("link", { name: /onboard first/i })).toHaveAttribute("href", "/chat");
    expect(screen.queryByTestId("join-form")).not.toBeInTheDocument();
  });

  it("onboarded, open, not joined: renders the join form", async () => {
    get.mockResolvedValue(me());
    mount();
    expect(await screen.findByTestId("join-form")).toBeInTheDocument();
  });

  it("not joined after the close: sheet-closed copy, no join form", async () => {
    get.mockResolvedValue(me({ phase: "closed" }));
    mount(info({ phase: "closed" }));
    expect(await screen.findByText(/The sheet has closed/)).toBeInTheDocument();
    expect(screen.queryByTestId("join-form")).not.toBeInTheDocument();
  });

  it("joined (open): effective position with the raw one secondary, referrals, credits, referral link", async () => {
    get.mockResolvedValue(joined());
    mount();
    expect(await screen.findByText(/You're #17/)).toBeInTheDocument();
    expect(screen.getByText(/#47 before referral jumps/)).toBeInTheDocument();
    expect(screen.getByText(/3 subscribed referrals — each one moves you up 10 spots/)).toBeInTheDocument();
    expect(screen.getByText(/5 subscribed referrals earn \$50 · 20 earn \$250 total\./)).toBeInTheDocument();
    expect(screen.getByLabelText(/your referral link/i)).toHaveValue("https://flickmeon.ai/?ref=P9AB3CDE");
  });

  it("joined with no jumps yet: no secondary raw position", async () => {
    get.mockResolvedValue(joined({ position: 47, effective_position: 47, referrals_subscribed: 0 }));
    mount();
    expect(await screen.findByText(/You're #47/)).toBeInTheDocument();
    expect(screen.queryByText(/before referral jumps/)).not.toBeInTheDocument();
  });

  it("milestone reached: names the earned credits", async () => {
    get.mockResolvedValue(joined({ referrals_subscribed: 6, credits_milestone_reached: 5 }));
    mount();
    expect(await screen.findByText(/You've earned \$50 in credits\./)).toBeInTheDocument();
  });

  it("winner post-close: free-month copy with the generic redemption line", async () => {
    get.mockResolvedValue(joined({ phase: "closed", winner: true }));
    mount(info({ phase: "closed" }));
    expect(await screen.findByText("You won a free month.")).toBeInTheDocument();
    expect(screen.getByText(/at checkout if you haven't subscribed yet, or on your next invoice/)).toBeInTheDocument();
  });

  it("joined but ineligible post-close: subscribe-to-keep-building copy, product unaffected", async () => {
    get.mockResolvedValue(joined({ phase: "closed", compete_eligible: false }));
    mount(info({ phase: "closed" }));
    expect(await screen.findByText(/The sheet closed — subscribe to keep building/)).toBeInTheDocument();
    expect(screen.queryByText("You won a free month.")).not.toBeInTheDocument();
  });

  it("competition phase, eligible: offers the Stripe connect form (board state is session-local)", async () => {
    get.mockResolvedValue(joined({ phase: "competition" }));
    mount(info({ phase: "competition" }));
    expect(await screen.findByText(/Connect a read-only Stripe key/)).toBeInTheDocument();
    expect(screen.getByTestId("connect-stripe-form")).toBeInTheDocument();
    // Referral jumps froze at close — the open-phase referral block is gone.
    expect(screen.queryByText(/moves you up/)).not.toBeInTheDocument();
  });

  it("attributes a stored referral once and clears it on a resolved response", async () => {
    get.mockResolvedValue(joined());
    post.mockResolvedValue({ attributed: true });
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    useLaunchRef.mockReturnValue("K7QX2MPP");
    mount();
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    await waitFor(() => expect(localStorage.getItem(LAUNCH_REF_KEY)).toBeNull());
  });

  it("never self-attributes the caller's own code", async () => {
    get.mockResolvedValue(joined({ referral_code: "K7QX2MPP" }));
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    mount();
    await screen.findByText(/You're #17/);
    expect(post).not.toHaveBeenCalled();
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("K7QX2MPP");
  });

  it("a reload mid-competition seeds board state from me.entry — lift + toggle, no connect invite", async () => {
    get.mockResolvedValue(joined({
      phase: "competition",
      entry: { has_key: true, public: true, entered_at: "t", currency: "eur", delta_cents: 5000, refreshed_at: "t", refresh_error: null },
    }));
    patch.mockResolvedValue({ public: false });
    mount(info({ phase: "competition" }));
    expect(await screen.findByText(/Lift so far/)).toBeInTheDocument();
    expect(screen.getByText("€50")).toBeInTheDocument();  // the entry's own currency, never a USD default
    expect(screen.queryByText(/Connect a read-only Stripe key/)).not.toBeInTheDocument();
    const checkbox = screen.getByLabelText(/show my name on the board/i);
    fireEvent.click(checkbox);
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/launch/entry", { public: false }));
  });

  it("a refresh_error on me.entry re-offers the connect form", async () => {
    get.mockResolvedValue(joined({
      phase: "competition",
      entry: { has_key: true, public: true, entered_at: "t", currency: "usd", delta_cents: 0, refreshed_at: "t", refresh_error: "stripe_auth" },
    }));
    mount(info({ phase: "competition" }));
    expect(await screen.findByText(/stripe_auth/)).toBeInTheDocument();
    expect(screen.getByTestId("connect-stripe-form")).toBeInTheDocument();
  });

  it("the public toggle appears only once an entry is known, and PATCHes /launch/entry", async () => {
    get.mockResolvedValue(joined({ phase: "competition" }));
    patch.mockResolvedValue({ public: false });
    mount(info({ phase: "competition" }));
    const connect = await screen.findByTestId("connect-stripe-form");
    expect(screen.queryByLabelText(/show my name on the board/i)).not.toBeInTheDocument();

    fireEvent.click(connect); // the mocked form hands back a connected entry
    expect(await screen.findByText(/Lift so far/)).toBeInTheDocument();
    expect(screen.getByText("$50")).toBeInTheDocument();
    const checkbox = screen.getByLabelText(/show my name on the board/i);
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/launch/entry", { public: false }));
  });
});
