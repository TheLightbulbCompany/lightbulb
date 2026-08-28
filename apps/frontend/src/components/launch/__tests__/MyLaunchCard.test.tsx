import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi, beforeEach } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post, put: vi.fn(), del: vi.fn() }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../EnterForm", () => ({ EnterForm: () => <div data-testid="enter-form" /> }));
const useLaunchRef = vi.fn<() => string | null>(() => null);
vi.mock("../useLaunchRef", () => ({ useLaunchRef: () => useLaunchRef() }));

import { LAUNCH_REF_KEY, type LaunchMe } from "@/lib/launch";
import { MyLaunchCard } from "../MyLaunchCard";

const info = { phase: "live" as const, drop_at: "", competition_start_at: "", competition_end_at: "", pot_cents: 0, entrants: 0, waitlist_count: 0, comp_slots: 220 };
const me = (over: Partial<LaunchMe>): LaunchMe => ({
  phase: "live",
  waitlist: { position: 47, referral_code: "P9AB3CDE", in_comp_window: true, referral_link: "https://flickmeon.ai/?ref=P9AB3CDE", referrals_signed_up: 3, referrals_paid: 1, free_months_banked: 1 },
  activated: true, activated_at: "2026-09-19T00:00:00Z", entry: null, can_enter: true, reason: null, ...over,
});

function mount(refCode: string | null = null) {
  useLaunchRef.mockReturnValue(refCode);
  return render(<SWRConfig value={{ provider: () => new Map() }}><MyLaunchCard info={info} /></SWRConfig>);
}

describe("MyLaunchCard", () => {
  beforeEach(() => { get.mockReset(); post.mockReset(); useLaunchRef.mockReturnValue(null); });

  it("renders position, referral stats and the enter form when eligible", async () => {
    get.mockResolvedValue(me({}));
    mount();
    expect(await screen.findByText(/#47/)).toBeInTheDocument();
    expect(screen.getByText(/3 signed up/)).toBeInTheDocument();
    expect(screen.getByText(/1 paid/)).toBeInTheDocument();
    expect(screen.getByText(/1 free month banked/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/ref=P9AB3CDE/)).toBeInTheDocument();
    expect(screen.getByTestId("enter-form")).toBeInTheDocument();
  });

  it("explains why entry is blocked", async () => {
    get.mockResolvedValue(me({ activated: false, can_enter: false, reason: "not_activated" }));
    mount();
    expect(await screen.findByText(/finish onboarding/i)).toBeInTheDocument();
    expect(screen.queryByTestId("enter-form")).not.toBeInTheDocument();
  });

  it("shows the entry summary once entered", async () => {
    get.mockResolvedValue(me({ entry: { display_name: "Ada", public: true, entered_at: "t", currency: "usd", baseline_cents: 10000, window_cents: 15000, delta_cents: 5000, refreshed_at: "t", refresh_error: null } }));
    mount();
    expect(await screen.findByText(/You're in as Ada/)).toBeInTheDocument();
    expect(screen.getByText("$50")).toBeInTheDocument();
  });

  it("attributes a stored referral once and clears it", async () => {
    get.mockResolvedValue(me({}));
    post.mockResolvedValue({ attributed: true });
    localStorage.setItem(LAUNCH_REF_KEY, "K7QX2MPP");
    mount("K7QX2MPP");
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "K7QX2MPP" }));
    await waitFor(() => expect(localStorage.getItem(LAUNCH_REF_KEY)).toBeNull());
  });
});
