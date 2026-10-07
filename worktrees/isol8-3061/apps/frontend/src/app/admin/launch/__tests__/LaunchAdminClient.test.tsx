import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
const setLaunchTeamEntry = vi.fn();
vi.mock("../../_actions/launch", () => ({
  refreshLaunch: () => refreshLaunch(),
  removeLaunchEntry: (ownerId: string) => removeLaunchEntry(ownerId),
  closeLaunchSheet: (force: boolean) => closeLaunchSheet(force),
  setLaunchTeamEntry: (ownerId: string, body: object) => setLaunchTeamEntry(ownerId, body),
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
  emails_winner: 30,
  emails_competing: 6,
  emails_not_selected: 4,
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
  closeLaunchSheet.mockResolvedValue({ ok: true, status: 202, data: { status: "started" } });
  setLaunchTeamEntry.mockReset();
  setLaunchTeamEntry.mockResolvedValue({ ok: true, status: 200, data: {} });
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
    pot_per_seat_cents: 500,
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
  pot_per_seat_cents: 500,
  projected_pot_cents: 140000,
  closed: false,
  closed_at: null,
  close_running: false,
  close_result: null,
  final_pot_cents: null,
  final_roster_count: null,
  referrals: { signed_up: 50, subscribed: 12 },
  teams: { total: 0, counted: 0, contribution_cents: 0 },
  team_entries: [],
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
    expect(
      screen.getByText(/\$1,000 \+ \$10 × roster \+ counted teams \(\$10 \+ \$5 × seats\) — never shown publicly/),
    ).toBeInTheDocument();
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

  it("Close the sheet requires typing 'close' before the POST fires, then shows the 202 in-progress state", async () => {
    const user = userEvent.setup();
    render(<LaunchAdminClient data={data} />);
    await user.click(screen.getByRole("button", { name: /close the sheet/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(closeLaunchSheet).not.toHaveBeenCalled();
    const input = within(dialog).getByLabelText(/type close to confirm/i);
    await user.type(input, "close");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));

    expect(closeLaunchSheet).toHaveBeenCalledWith(false);
    // 202 — the close runs in the background; the card says so.
    expect((await screen.findAllByText(/Closing…/)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /retry \(force\)/i })).not.toBeInTheDocument();
  });

  it("a running close (from the sheet payload) shows the in-progress line", () => {
    render(<LaunchAdminClient data={{ ...data, close_running: true }} />);
    expect(screen.getAllByText(/Closing…/).length).toBeGreaterThan(0);
  });

  it("no close button of ANY kind while a run is live — a disabled in-progress state instead", () => {
    // Forcing a second concurrent close from the UI is never right: the
    // top-level force door must vanish while close_running (and the first
    // Close button with it).
    render(<LaunchAdminClient data={{ ...data, closed: true, closed_at: "t", close_running: true }} />);
    expect(screen.queryByRole("button", { name: /re-run close \(force\)/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /close the sheet/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /close in progress…/i })).toBeDisabled();
  });

  it("a finished close renders the persisted summary counts", () => {
    render(<LaunchAdminClient data={{ ...data, closed: true, closed_at: closeResult.closed_at, close_result: closeResult }} />);
    expect(screen.getByText(/40 winners flagged of 40 on the sheet/)).toBeInTheDocument();
    expect(screen.getByText(/pot \$1,400/)).toBeInTheDocument();
    // Every cohort's send count is visible — a total alone hides who heard what.
    expect(screen.getByText(/emails 40 sent \(30 winner \/ 6 competing \/ 4 not selected\)/)).toBeInTheDocument();
    // Clean close: no retry affordance, no skipped warning.
    expect(screen.queryByRole("button", { name: /retry \(force\)/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/not on the Workspace plan/)).not.toBeInTheDocument();
  });

  it("coupon/email failures in the persisted result surface a Retry (force) that re-POSTs with force:true", async () => {
    const user = userEvent.setup();
    render(
      <LaunchAdminClient
        data={{ ...data, closed: true, close_result: { ...closeResult, coupon_failures: 2, email_failures: 1 } }}
      />,
    );
    const retry = await screen.findByRole("button", { name: /retry \(force\)/i });
    await user.click(retry);
    expect(closeLaunchSheet).toHaveBeenLastCalledWith(true);
  });

  it("skipped non-Workspace paying winners get a visible warning", () => {
    render(
      <LaunchAdminClient
        data={{ ...data, closed: true, close_result: { ...closeResult, coupon_skipped_nonflat: 3 } }}
      />,
    );
    expect(screen.getByText(/3 paying winner\(s\) are not on the Workspace plan/)).toBeInTheDocument();
    expect(screen.getByText(/3 skipped \(non-Workspace\)/)).toBeInTheDocument();
  });

  it("a failed background run surfaces its error", () => {
    render(<LaunchAdminClient data={{ ...data, closed: true, close_result: { error: "stripe_error" } }} />);
    expect(screen.getByText(/Close run failed \(stripe_error\)/)).toBeInTheDocument();
  });

  it("a 422 renders the field errors, never [object Object]", async () => {
    const user = userEvent.setup();
    closeLaunchSheet.mockResolvedValue({
      ok: false,
      status: 422,
      error: "http_422",
      data: { detail: { errors: { sheet_closes_at: "must be in the past", comp_slots: "must be at least 1" } } },
    });
    render(<LaunchAdminClient data={data} />);
    await user.click(screen.getByRole("button", { name: /close the sheet/i }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/type close to confirm/i), "close");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));
    expect(
      await screen.findByText(/Close failed \(422\): sheet_closes_at: must be in the past; comp_slots: must be at least 1/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/object Object/)).not.toBeInTheDocument();
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

describe("TeamEntriesCard", () => {
  const withTeam = (): LaunchOpsSheet => ({
    ...data,
    teams: { total: 1, counted: 0, contribution_cents: 0 },
    team_entries: [
      {
        owner_id: "org_1",
        team_name: "Tuple",
        seats: 4,
        note: "six of us",
        submitter_email: "founder@tuple.com",
        submitter_user_id: "u_t",
        submitter_org_slug: "tuple",
        counted: false,
        submitted_at: "2026-09-01T00:00:00Z",
        updated_at: "2026-09-02T00:00:00Z",
        contribution_cents: 1_000 + 4 * 500,
      },
    ],
  });

  it("shows what a submission would add to the pot before it is counted", () => {
    render(<LaunchAdminClient data={withTeam()} />);
    expect(screen.getByText("Team entries (0 of 1 counted)")).toBeInTheDocument();
    // Scoped to the team's own row, so it can never pass off another table.
    const row = within(screen.getByText("Tuple").closest("tr")!);
    expect(row.getByText("$30")).toBeInTheDocument(); // $10 + 4 × $5
    // submitted_at survives a resubmission, so the edited column is the only
    // thing that moves when someone changes their details after review.
    expect(row.getByText("2026-09-02T00:00:00Z")).toBeInTheDocument();
  });

  it("the counts-toward-the-pot toggle posts for that owner", async () => {
    render(<LaunchAdminClient data={withTeam()} />);
    fireEvent.click(screen.getByLabelText("Count Tuple toward the pot"));
    await waitFor(() => expect(setLaunchTeamEntry).toHaveBeenCalledWith("org_1", { counted: true }));
  });

  it("a corrected seat count posts on blur, and only when it changed", async () => {
    render(<LaunchAdminClient data={withTeam()} />);
    const seats = screen.getByLabelText("Seats for Tuple");
    fireEvent.blur(seats);
    expect(setLaunchTeamEntry).not.toHaveBeenCalled();

    fireEvent.change(seats, { target: { value: "6" } });
    fireEvent.blur(seats);
    await waitFor(() => expect(setLaunchTeamEntry).toHaveBeenCalledWith("org_1", { seats: 6 }));
  });

  it("surfaces a 422 field error instead of silently keeping the typed number", async () => {
    setLaunchTeamEntry.mockResolvedValue({
      ok: false,
      status: 422,
      data: { detail: { errors: { seats: "must be between 1 and 500" } } },
    });
    render(<LaunchAdminClient data={withTeam()} />);
    const seats = screen.getByLabelText("Seats for Tuple");
    fireEvent.change(seats, { target: { value: "900" } });
    fireEvent.blur(seats);
    expect(await screen.findByText(/seats: must be between 1 and 500/)).toBeInTheDocument();
  });
});
