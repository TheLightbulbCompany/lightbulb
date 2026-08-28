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

import type { LaunchOpsData } from "../../_lib/api";
import { LaunchAdminClient } from "../LaunchAdminClient";

const data: LaunchOpsData = {
  generated_at: "t", phase: "live",
  schedule: { drop_at: "d", competition_start_at: "s", competition_end_at: "e", comp_slots: 220 },
  waitlist: { total: 300, top_slots_activated: 150, top_slots_claimed: 200 },
  entrants: { total: 40, paid: 30, comp_eligible: 10, public: 35 },
  pot_cents: 140000,
  referrals: { signed_up: 50, paid: 12, months_banked: 3 },
  economics: { paid_profit_cents: 66000, comp_cost_cents: 28000, referral_cost_cents: 33600, pnl_cents: 4400, aws_credits_cents: 500000, net_cents: 504400 },
  entries: [{ owner_id: "u1", display_name: "Ada", public: true, waitlist_position: 3, currency: "usd", baseline_cents: 1000, window_cents: 3000, delta_cents: 2000, pct: 2, refreshed_at: "t", refresh_error: null, comp_eligible: true }],
  waitlist_rows: [{ email: "a@x.com", position: 1, referral_link: "https://flickmeon.ai/?ref=A", created_at: "t", user_id: "u1", activated: true, referred_by: null, referrals_signed_up: 0, referrals_paid: 0, free_months_banked: 0 }],
};

describe("LaunchAdminClient", () => {
  it("renders the sheet numbers and the entries table", () => {
    render(<LaunchAdminClient data={data} />);
    expect(screen.getByText("$1,400")).toBeInTheDocument();
    expect(screen.getByText("150 / 220")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("+200%")).toBeInTheDocument();
    // "eligible", not "comped": redemption of the promo code is not observed.
    expect(screen.getByText("eligible")).toBeInTheDocument();
    expect(screen.getByText(/30 paid · 10 eligible · 35 public/)).toBeInTheDocument();
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
});
