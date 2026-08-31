import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
const refreshLaunch = vi.fn();
const removeLaunchEntry = vi.fn();
const closeLaunchSheet = vi.fn();
vi.mock("../../_actions/launch", () => ({
  refreshLaunch: () => refreshLaunch(),
  removeLaunchEntry: (ownerId: string) => removeLaunchEntry(ownerId),
  closeLaunchSheet: (force: boolean) => closeLaunchSheet(force),
}));

const closeResult = {
  closed_at: "2026-09-30T16:00:00+00:00",
  sheet_count: 40,
  winners: 40,
  paying: 12,
  final_roster_count: 40,
  final_pot_cents: 140000,
  coupons_attached: 12,
  coupon_failures: 0,
  emails_sent: 40,
  email_failures: 0,
};

// tests/setup.ts runs a global `afterEach(() => { vi.restoreAllMocks(); ... })`, which
// wipes a `vi.fn().mockResolvedValue(...)` configured once at module scope back to a
// no-op after the first test — re-arm before every test.
beforeEach(() => {
  refreshLaunch.mockReset();
  refreshLaunch.mockResolvedValue({ ok: true, status: 200, data: { refreshed: 2 } });
  removeLaunchEntry.mockReset();
  removeLaunchEntry.mockResolvedValue({ ok: true, status: 200, data: { deleted: "u1" } });
  closeLaunchSheet.mockReset();
  closeLaunchSheet.mockResolvedValue({ ok: true, status: 200, data: closeResult });
});

import type { LaunchOpsSheet, LaunchSheetRow } from "../../_lib/api";
import { csvEscape, LaunchAdminClient, sheetCsv } from "../LaunchAdminClient";

const sheetRow = (over: Partial<LaunchSheetRow> = {}): LaunchSheetRow => ({
  email: "a@x.com",
  position: 4,
  effective_position: 1,
  display_name: "Ada",
  public: true,
  referral_link: "https://flickmeon.ai/?ref=A",
  created_at: "c",
  joined_at: "j",
  user_id: "u1",
  referred_by: null,
  referrals_signed_up: 4,
  referrals_subscribed: 3,
  winner: true,
  compete_eligible: true,
  coupon_attached_at: "t",
  winner_email_sent_at: "t",
  ...over,
});

const data: LaunchOpsSheet = {
  generated_at: "t",
  phase: "open",
  settings: {
    enabled: true,
    sheet_closes_at: "2026-09-30T12:00:00-04:00",
    competition_start_at: "2026-10-01T00:00:00-04:00",
    competition_end_at: "2026-12-01T00:00:00-05:00",
    comp_slots: 200,
    pot_front_cents: 100000,
    pot_per_entrant_cents: 1000,
    referral_jump: 10,
    stripe_coupon_id: "co_1",
    closed_at: null,
    final_pot_cents: null,
    final_roster_count: null,
    updated_at: "2026-08-01T00:00:00Z",
  },
  enabled: true,
  schedule: { sheet_closes_at: "s", competition_start_at: "cs", competition_end_at: "ce", comp_slots: 200 },
  signups: 40,
  sheet: { total: 60, joined: 40, winners: 0 },
  competing: 12,
  on_board: 8,
  pot_front_cents: 100000,
  pot_per_entrant_cents: 1000,
  projected_pot_cents: 140000,
  closed: false,
  closed_at: null,
  final_pot_cents: null,
  final_roster_count: null,
  referrals: { signed_up: 50, subscribed: 12 },
  entries: [{ owner_id: "u1", has_subscription: true, display_name: "Ada", public: true, waitlist_position: 3, currency: "usd", baseline_cents: 1000, window_cents: 3000, delta_cents: 2000, pct: 2, refreshed_at: "t", refresh_error: null }],
  sheet_rows: [sheetRow(), sheetRow({ email: "b@x.com", position: 2, effective_position: 2, display_name: "Bob", user_id: "u2", winner: false, compete_eligible: false, coupon_attached_at: null, winner_email_sent_at: null, referrals_subscribed: 0, referrals_signed_up: 0 })],
};

describe("LaunchAdminClient", () => {
  it("renders the sheet numbers, the sheet table, and the entries table", () => {
    render(<LaunchAdminClient data={data} />);
    expect(screen.getByText("signups")).toBeInTheDocument();
    expect(screen.getByText("on the sheet")).toBeInTheDocument();
    // The projected pot is labelled internal and hints its own arithmetic.
    expect(screen.getByText("projected pot (internal)")).toBeInTheDocument();
    expect(screen.getByText("$1,400")).toBeInTheDocument();
    expect(screen.getByText(/\$1,000 \+ \$10 × roster — never shown publicly/)).toBeInTheDocument();
    // Sheet table: effective position first, winner/eligible flags per row.
    // ("Ada" is in the sheet table AND the board entries table.)
    expect(screen.getAllByText("Ada").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Bob")).toBeInTheDocument();
    expect(screen.getByText("referrals subscribed")).toBeInTheDocument();
    // Board entry renders the growth pct.
    expect(screen.getByText("+200%")).toBeInTheDocument();
  });

  it("no closed card and no force-rerun button while the sheet is open", () => {
    render(<LaunchAdminClient data={data} />);
    expect(screen.queryByText("Sheet closed")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /re-run close/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /close the sheet/i })).toBeInTheDocument();
  });

  it("Close the sheet requires typing 'close' before the POST fires, then surfaces the counts", async () => {
    const user = userEvent.setup();
    render(<LaunchAdminClient data={data} />);
    await user.click(screen.getByRole("button", { name: /close the sheet/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(closeLaunchSheet).not.toHaveBeenCalled();
    const input = within(dialog).getByLabelText(/type close to confirm/i);
    await user.type(input, "close");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));

    expect(closeLaunchSheet).toHaveBeenCalledWith(false);
    expect(await screen.findByText(/40 winners flagged of 40 on the sheet/)).toBeInTheDocument();
    expect(screen.getByText(/pot \$1,400/)).toBeInTheDocument();
    // Clean close: no retry affordance.
    expect(screen.queryByRole("button", { name: /retry \(force\)/i })).not.toBeInTheDocument();
  });

  it("coupon/email failures surface a Retry (force) that re-POSTs with force:true", async () => {
    const user = userEvent.setup();
    closeLaunchSheet.mockResolvedValue({ ok: true, status: 200, data: { ...closeResult, coupon_failures: 2, email_failures: 1 } });
    render(<LaunchAdminClient data={data} />);
    await user.click(screen.getByRole("button", { name: /close the sheet/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/type close to confirm/i), "close");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));

    const retry = await screen.findByRole("button", { name: /retry \(force\)/i });
    closeLaunchSheet.mockResolvedValue({ ok: true, status: 200, data: closeResult });
    await user.click(retry);
    expect(closeLaunchSheet).toHaveBeenLastCalledWith(true);
  });

  it("a 409 already_closed surfaces as an error, not a silent no-op", async () => {
    const user = userEvent.setup();
    closeLaunchSheet.mockResolvedValue({ ok: false, status: 409, error: "already_closed" });
    render(<LaunchAdminClient data={data} />);
    await user.click(screen.getByRole("button", { name: /close the sheet/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/type close to confirm/i), "close");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));
    expect(await screen.findByText(/Close failed \(409\): already_closed/)).toBeInTheDocument();
  });

  it("closed state renders prominently with the frozen numbers and the force-rerun door", () => {
    render(
      <LaunchAdminClient
        data={{ ...data, closed: true, closed_at: "2026-09-30T16:00:00+00:00", final_pot_cents: 141000, final_roster_count: 41 }}
      />,
    );
    expect(screen.getByText("Sheet closed")).toBeInTheDocument();
    expect(screen.getByText("2026-09-30T16:00:00+00:00")).toBeInTheDocument();
    expect(screen.getByText("41")).toBeInTheDocument();
    // The frozen pot shows in the closed card AND the metric tile.
    expect(screen.getAllByText("$1,410").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("button", { name: /^close the sheet$/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /re-run close \(force\)/i })).toBeInTheDocument();
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

  it("builds the CSV blob from the sheet rows", () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    render(<LaunchAdminClient data={data} />);
    fireEvent.click(screen.getByRole("button", { name: /export sheet csv/i }));
    const blob = create.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("text/csv");
  });
});

describe("sheetCsv", () => {
  it("carries the new columns in the payload's own order", () => {
    const csv = sheetCsv(data.sheet_rows);
    expect(csv.split("\n")[0]).toBe(
      "email,position,effective_position,display_name,public,referral_link,created_at,joined_at,user_id,referred_by,referrals_signed_up,referrals_subscribed,winner,compete_eligible,coupon_attached_at,winner_email_sent_at",
    );
    expect(csv).toContain("a@x.com,4,1,Ada,true,https://flickmeon.ai/?ref=A,c,j,u1,,4,3,true,true,t,t");
    expect(csv).toContain("b@x.com,2,2,Bob,true,https://flickmeon.ai/?ref=A,c,j,u2,,0,0,false,false,,");
  });
});

describe("csvEscape", () => {
  // A bare =/+/-/@ leading char is a formula trigger when the CSV is opened
  // in Excel/Sheets — prefix with a `'` before the quoting decision.
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
