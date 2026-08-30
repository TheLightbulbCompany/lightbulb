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
  // LaunchAdminClient.test.tsx mocks the same module for its own two
  // actions — both files mock this module independently, so the unused
  // exports below just keep the mock shape complete for either importer.
  refreshLaunch: vi.fn(),
  removeLaunchEntry: vi.fn(),
}));

import type { LaunchSettings } from "../../_lib/api";
import { LaunchSettingsCard } from "../LaunchSettingsCard";

const baseSettings: LaunchSettings = {
  enabled: false,
  drop_at: "2026-07-15T10:00:00-04:00",
  competition_start_at: "2026-07-20T10:00:00-04:00",
  comp_slots: 220,
  competition_days: 30,
  pot_front_cents: 100000,
  pot_per_entrant_cents: 1000,
  stripe_coupon_id: "",
  stripe_promotion_code_id: "",
  stripe_promotion_code: "",
  updated_at: "2026-07-01T00:00:00Z",
};

beforeEach(() => {
  mockRefresh.mockReset();
  mockSave.mockReset();
  mockSave.mockResolvedValue({ ok: true, status: 200, data: baseSettings });
  mockMintCoupon.mockReset();
  mockMintCoupon.mockResolvedValue({
    ok: true,
    status: 200,
    data: { stripe_coupon_id: "co_1", stripe_promotion_code_id: "promo_1", stripe_promotion_code: "LIGHTBULB-ABC123" },
  });
});

describe("LaunchSettingsCard", () => {
  it("renders the settings row converted into ET wall time / dollars", () => {
    render(<LaunchSettingsCard settings={baseSettings} enabled={baseSettings.enabled} />);
    expect(screen.getByLabelText("Drop (ET)")).toHaveValue("2026-07-15T10:00");
    expect(screen.getByLabelText("Competition start (ET)")).toHaveValue("2026-07-20T10:00");
    expect(screen.getByLabelText(/comp slots/i)).toHaveValue(220);
    expect(screen.getByLabelText(/competition days/i)).toHaveValue(30);
    expect(screen.getByLabelText(/pot front/i)).toHaveValue(1000);
    expect(screen.getByLabelText(/pot per entrant/i)).toHaveValue(10);
    expect(screen.getByRole("checkbox", { name: /enabled/i })).not.toBeChecked();
  });

  it("saves with dates converted back to ISO and dollars converted to cents, with no confirm gate when enabled/phase don't change", async () => {
    render(<LaunchSettingsCard settings={baseSettings} enabled={baseSettings.enabled} />);
    fireEvent.change(screen.getByLabelText(/pot front/i), { target: { value: "1200" } });

    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText(/^saved\.$/i)).toBeInTheDocument();
    expect(mockSave).toHaveBeenCalledWith({
      enabled: false,
      drop_at: "2026-07-15T10:00:00-04:00",
      competition_start_at: "2026-07-20T10:00:00-04:00",
      comp_slots: 220,
      competition_days: 30,
      pot_front_cents: 120000,
      pot_per_entrant_cents: 1000,
    });
    expect(mockRefresh).toHaveBeenCalled();
  });

  it("renders 422 field errors under the matching input", async () => {
    mockSave.mockResolvedValue({
      ok: false,
      status: 422,
      data: { detail: { errors: { comp_slots: "must be at least 1" } } },
    });
    render(<LaunchSettingsCard settings={baseSettings} enabled={baseSettings.enabled} />);
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText("must be at least 1")).toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("gates a save that flips `enabled` behind the typed-launch confirm dialog", async () => {
    const user = userEvent.setup();
    render(<LaunchSettingsCard settings={baseSettings} enabled={baseSettings.enabled} />);

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

  it("disables the coupon button until both dates are set", () => {
    render(
      <LaunchSettingsCard
        settings={{ ...baseSettings, drop_at: null, competition_start_at: null }}
        enabled={false}
      />,
    );
    expect(screen.getByRole("button", { name: /create stripe coupon/i })).toBeDisabled();
  });

  it("mints the coupon directly (no confirm) when none exists yet", async () => {
    const user = userEvent.setup();
    render(<LaunchSettingsCard settings={baseSettings} enabled={baseSettings.enabled} />);
    await user.click(screen.getByRole("button", { name: /create stripe coupon/i }));
    expect(mockMintCoupon).toHaveBeenCalledWith(false);
    expect(await screen.findByText(/coupon \+ promo code created/i)).toBeInTheDocument();
  });

  it("requires the typed 'create' confirm and passes force:true when a coupon already exists", async () => {
    const user = userEvent.setup();
    render(
      <LaunchSettingsCard
        settings={{ ...baseSettings, stripe_coupon_id: "co_existing" }}
        enabled={baseSettings.enabled}
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
