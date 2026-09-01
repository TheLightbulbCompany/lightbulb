import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockRefresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mockRefresh }) }));

const mockSave = vi.fn();
const mockMintCoupon = vi.fn();
vi.mock("../../_actions/launch", () => ({
  saveLaunchSettings: (...args: unknown[]) => mockSave(...args),
  createLaunchStripeCoupon: (...args: unknown[]) => mockMintCoupon(...args),
  // LaunchAdminClient.test.tsx mocks the same module for its own actions —
  // both files mock it independently, so the unused exports below just keep
  // the mock shape complete for either importer.
  refreshLaunch: vi.fn(),
  removeLaunchEntry: vi.fn(),
  closeLaunchSheet: vi.fn(),
}));

import type { LaunchSettings } from "../../_lib/api";
import { LaunchSettingsCard } from "../LaunchSettingsCard";

const baseSettings: LaunchSettings = {
  enabled: false,
  sheet_closes_at: "2026-09-30T12:00:00-04:00",
  competition_start_at: "2026-10-01T00:00:00-04:00",
  competition_end_at: "2026-12-01T00:00:00-05:00",
  comp_slots: 200,
  pot_front_cents: 100000,
  pot_per_seat_cents: 500,
  pot_per_entrant_cents: 1000,
  referral_jump: 10,
  stripe_coupon_id: "",
  closed_at: null,
  final_pot_cents: null,
  final_roster_count: null,
  updated_at: "2026-07-01T00:00:00Z",
};

beforeEach(() => {
  mockRefresh.mockReset();
  mockSave.mockReset();
  mockSave.mockResolvedValue({ ok: true, status: 200, data: baseSettings });
  mockMintCoupon.mockReset();
  mockMintCoupon.mockResolvedValue({ ok: true, status: 200, data: { stripe_coupon_id: "co_1" } });
});

describe("LaunchSettingsCard", () => {
  it("renders the settings row converted into ET wall time / dollars, pot fields marked internal", () => {
    render(<LaunchSettingsCard settings={baseSettings} />);
    expect(screen.getByLabelText("Sheet closes (ET)")).toHaveValue("2026-09-30T12:00");
    expect(screen.getByLabelText("Competition start (ET)")).toHaveValue("2026-10-01T00:00");
    expect(screen.getByLabelText("Competition end (ET)")).toHaveValue("2026-12-01T00:00");
    expect(screen.getByLabelText(/comp slots/i)).toHaveValue(200);
    expect(screen.getByLabelText(/pot front/i)).toHaveValue(1000);
    expect(screen.getByLabelText(/pot per entrant/i)).toHaveValue(10);
    expect(screen.getByLabelText(/spots per referral/i)).toHaveValue(10);
    expect(screen.getByText(/internal — never shown publicly/i)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /enabled/i })).not.toBeChecked();
  });

  it("saves the exact 8-field body: dates back to ISO, dollars to cents, no confirm gate for pot tweaks", async () => {
    render(<LaunchSettingsCard settings={baseSettings} />);
    fireEvent.change(screen.getByLabelText(/pot front/i), { target: { value: "1200" } });

    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/^saved\.$/i)).toBeInTheDocument();
    expect(mockSave).toHaveBeenCalledWith({
      enabled: false,
      sheet_closes_at: "2026-09-30T12:00:00-04:00",
      competition_start_at: "2026-10-01T00:00:00-04:00",
      competition_end_at: "2026-12-01T00:00:00-05:00",
      comp_slots: 200,
      pot_front_cents: 120000,
      pot_per_seat_cents: 500,
  pot_per_entrant_cents: 1000,
      referral_jump: 10,
    });
    expect(mockRefresh).toHaveBeenCalled();
  });

  it("renders 422 field errors under the matching input", async () => {
    mockSave.mockResolvedValue({
      ok: false,
      status: 422,
      data: { detail: { errors: { comp_slots: "must be at least 1" } } },
    });
    render(<LaunchSettingsCard settings={baseSettings} />);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText("must be at least 1")).toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("gates a save that flips `enabled` behind the typed-launch confirm dialog", async () => {
    const user = userEvent.setup();
    render(<LaunchSettingsCard settings={baseSettings} />);

    await user.click(screen.getByRole("checkbox", { name: /enabled/i }));
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("launch")).toBeInTheDocument();
    expect(mockSave).not.toHaveBeenCalled();

    const input = within(dialog).getByLabelText(/type launch to confirm/i);
    await user.type(input, "launch");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));

    expect(mockSave).toHaveBeenCalledWith(expect.objectContaining({ enabled: true }));
  });

  it("rounds the integer fields so a typed decimal never reaches the server", async () => {
    render(<LaunchSettingsCard settings={baseSettings} />);
    fireEvent.change(screen.getByLabelText(/comp slots/i), { target: { value: "1.5" } });
    fireEvent.change(screen.getByLabelText(/spots per referral/i), { target: { value: "7.6" } });

    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/^saved\.$/i)).toBeInTheDocument();
    expect(mockSave).toHaveBeenCalledWith(expect.objectContaining({ comp_slots: 2, referral_jump: 8 }));
  });

  it("reads the checkbox and the confirm gate off the saved row, not a derived flag", async () => {
    // An enabled row whose sheet failed to compute still renders enabled —
    // and saving it unchanged is not a "changes what every user sees" moment.
    const user = userEvent.setup();
    render(<LaunchSettingsCard settings={{ ...baseSettings, enabled: true }} />);
    expect(screen.getByRole("checkbox", { name: /enabled/i })).toBeChecked();

    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(mockSave).toHaveBeenCalledWith(expect.objectContaining({ enabled: true }));
  });

  it("mints the coupon directly (no confirm) when none exists yet", async () => {
    const user = userEvent.setup();
    render(<LaunchSettingsCard settings={baseSettings} />);
    await user.click(screen.getByRole("button", { name: /create stripe coupon/i }));
    expect(mockMintCoupon).toHaveBeenCalledWith(false);
    expect(await screen.findByText(/coupon created/i)).toBeInTheDocument();
  });

  it("requires the typed 'create' confirm and passes force:true when a coupon already exists", async () => {
    const user = userEvent.setup();
    render(
      <LaunchSettingsCard
        settings={{ ...baseSettings, stripe_coupon_id: "co_existing" }}
      />,
    );
    await user.click(screen.getByRole("button", { name: /create stripe coupon/i }));
    const dialog = await screen.findByRole("alertdialog");
    const input = within(dialog).getByLabelText(/type create to confirm/i);
    await user.type(input, "create");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));
    expect(mockMintCoupon).toHaveBeenCalledWith(true);
  });
});
