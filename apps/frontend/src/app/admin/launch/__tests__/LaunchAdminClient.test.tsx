import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// jsdom has no object-URL impl — stub it (repo convention, see ArtifactsPanel.test.tsx
// / MarkdownView.test.tsx) so `vi.spyOn(URL, "createObjectURL")` below has something
// to wrap; plain functions (not vi.fn()) so the global afterEach's restoreAllMocks
// doesn't remove the property itself between tests.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "";
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = () => undefined;
}

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const refreshLaunch = vi.fn().mockResolvedValue({ ok: true, status: 200, data: { refreshed: 2 } });
const removeLaunchEntry = vi.fn().mockResolvedValue({ ok: true, status: 200, data: { deleted: "u1" } });
vi.mock("../../_actions/launch", () => ({
  refreshLaunch: () => refreshLaunch(),
  removeLaunchEntry: (ownerId: string) => removeLaunchEntry(ownerId),
}));

// tests/setup.ts runs a global `afterEach(() => { vi.restoreAllMocks(); ... })`, which
// wipes a `vi.fn().mockResolvedValue(...)` configured once at module scope back to a
// no-op after the first test — same pattern already handled in
// ProvisioningActionsPanel.test.tsx. Re-arm before every test.
beforeEach(() => {
  refreshLaunch.mockReset();
  refreshLaunch.mockResolvedValue({ ok: true, status: 200, data: { refreshed: 2 } });
  removeLaunchEntry.mockReset();
  removeLaunchEntry.mockResolvedValue({ ok: true, status: 200, data: { deleted: "u1" } });
});

import type { LaunchOpsSheet } from "../../_lib/api";
import { csvEscape, LaunchAdminClient, waitlistCsv } from "../LaunchAdminClient";

const data: LaunchOpsSheet = {
  generated_at: "t", phase: "live",
  schedule: { drop_at: "d", competition_start_at: "s", competition_end_at: "e", comp_slots: 220 },
  waitlist: { total: 300, top_slots_activated: 150, top_slots_claimed: 200 },
  signups: 40,
  competing: 40,
  on_board: 25,
  competing_breakdown: { paid: 30, comp_eligible: 10, public: 35 },
  public_pot_cents: 140000,
  funded_pot_cents: 125000,
  pot_counted: 24,
  pot_front_cents: 100000,
  pot_per_entrant_cents: 1000,
  referrals: { signed_up: 50, paid: 12, months_banked: 3 },
  economics: { paid_profit_cents: 66000, comp_cost_cents: 28000, referral_cost_cents: 33600, pnl_cents: 4400, aws_credits_cents: 500000, net_cents: 504400 },
  entries: [{ owner_id: "u1", display_name: "Ada", public: true, waitlist_position: 3, currency: "usd", baseline_cents: 1000, window_cents: 3000, delta_cents: 2000, pct: 2, refreshed_at: "t", refresh_error: null, comp_eligible: true }],
  waitlist_rows: [{ email: "a@x.com", position: 1, effective_position: 1, referral_link: "https://flickmeon.ai/?ref=A", created_at: "t", user_id: "u1", activated: true, referred_by: null, referrals_signed_up: 0, referrals_paid: 0, free_months_banked: 0 }],
  settings: {
    enabled: true,
    drop_at: "2026-09-18T00:00:00-04:00",
    competition_start_at: "2026-09-25T00:00:00-04:00",
    comp_slots: 220,
    competition_days: 30,
    pot_front_cents: 100000,
    pot_per_entrant_cents: 1000,
    referral_jump: 10,
    stripe_coupon_id: "co_1",
    stripe_promotion_code_id: "promo_1",
    stripe_promotion_code: "LIGHTBULB-ABC123",
    updated_at: "2026-08-01T00:00:00Z",
  },
  enabled: true,
};

describe("LaunchAdminClient", () => {
  it("renders the sheet numbers and the entries table", () => {
    render(<LaunchAdminClient data={data} />);
    // public pot ($1,400) and funded pot ($1,250) — the two tiles replacing the old single "pot".
    expect(screen.getByText("$1,400")).toBeInTheDocument();
    expect(screen.getByText("$1,250")).toBeInTheDocument();
    expect(screen.getByText("150 / 220")).toBeInTheDocument();
    // The pot hint renders the schedule's own constants, not a hardcoded sum.
    expect(screen.getByText("40 signed up × $10 + $1,000")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("+200%")).toBeInTheDocument();
    // "eligible", not "comped": redemption of the promo code is not observed.
    expect(screen.getByText("eligible")).toBeInTheDocument();
    expect(screen.getByText(/30 paid · 10 eligible · 35 public/)).toBeInTheDocument();
    // signed up / competing / on the board counts
    expect(screen.getByText("signed up")).toBeInTheDocument();
    expect(screen.getByText("on the board")).toBeInTheDocument();
    expect(screen.getByText("25")).toBeInTheDocument();
  });

  it("refresh calls the action", async () => {
    render(<LaunchAdminClient data={data} />);
    fireEvent.click(screen.getByRole("button", { name: /refresh all/i }));
    expect(refreshLaunch).toHaveBeenCalled();
    expect(await screen.findByText(/refreshed 2/i)).toBeInTheDocument();
  });

  it("removes an entry from the board", async () => {
    render(<LaunchAdminClient data={data} />);
    fireEvent.click(screen.getByRole("button", { name: /^remove$/i }));
    expect(removeLaunchEntry).toHaveBeenCalledWith("u1");
    expect(await screen.findByText(/removed u1/i)).toBeInTheDocument();
  });

  it("builds a CSV from the waitlist rows", () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    render(<LaunchAdminClient data={data} />);
    fireEvent.click(screen.getByRole("button", { name: /export waitlist csv/i }));
    const blob = create.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("text/csv");
  });

  it("exports an unresolved activation as an em dash, never null", () => {
    expect(waitlistCsv(data.waitlist_rows)).toContain(",true");
    expect(waitlistCsv([{ ...data.waitlist_rows[0], activated: null }])).toContain(",—");
  });

  it("puts effective_position right after position", () => {
    const csv = waitlistCsv(data.waitlist_rows);
    expect(csv.split("\n")[0]).toBe("email,position,effective_position,referral_link,created_at,activated");
    expect(csv).toContain("a@x.com,1,1,https://flickmeon.ai/?ref=A,t,true");
  });
});

describe("csvEscape", () => {
  // CodeRabbit: a bare =/+/-/@ leading char is a formula trigger when the CSV
  // is opened in Excel/Sheets — prefix with a `'` before the quoting decision.
  it.each(["=cmd", "+1", "-1", "@SUM(A1)"])("neutralizes a leading %s", (value) => {
    expect(csvEscape(value)).toBe(`'${value}`);
  });

  it("still quotes commas/quotes/newlines, after neutralizing", () => {
    expect(csvEscape("=a,b")).toBe(`"'=a,b"`);
    expect(csvEscape('say "hi"')).toBe('"say ""hi"""');
  });

  it("leaves an ordinary value untouched", () => {
    expect(csvEscape("Ada")).toBe("Ada");
    expect(csvEscape(null)).toBe("");
  });
});
