"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { DefRow } from "@/components/admin/DefRow";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";

import { createLaunchStripeCoupon, saveLaunchSettings } from "../_actions/launch";
import type { LaunchSettings, LaunchSettingsInput } from "../_lib/api";
import { fromEasternLocal, toEasternLocal } from "./etTime";

type SaveResult = Awaited<ReturnType<typeof saveLaunchSettings>>;

/** 422s from both `/ops/launch/settings` and `/ops/launch/settings/stripe-coupon`
 * ride the same `{detail: {errors: {field: message}}}` envelope. */
function fieldErrorsFrom(result: SaveResult): Record<string, string> | null {
  if (result.status !== 422 || !result.data || typeof result.data !== "object") return null;
  const detail = (result.data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || !("errors" in detail)) return null;
  return (detail as { errors?: Record<string, string> }).errors ?? null;
}

function describeError(result: SaveResult): string {
  const fieldErrors = fieldErrorsFrom(result);
  if (fieldErrors) {
    return Object.entries(fieldErrors)
      .map(([field, msg]) => `${field}: ${msg}`)
      .join("; ");
  }
  return result.error ?? `http_${result.status}`;
}

export interface LaunchSettingsCardProps {
  settings: LaunchSettings;
}

/**
 * The admin editor for the launch-settings DynamoDB row — the three schedule
 * instants, comp slots, referral jump, the (internal-only) pot constants, and
 * the one-click Stripe coupon mint. Always rendered
 * (`app/admin/launch/page.tsx` mounts it regardless of `enabled`, since an
 * admin has to be able to turn the feature ON from here); the rest of the
 * sheet only shows up once the row is enabled and computes clean.
 */
export function LaunchSettingsCard({ settings }: LaunchSettingsCardProps) {
  const router = useRouter();

  // The saved row itself, never the sheet's derived `enabled` — those differ
  // whenever the row is on but the sheet failed to compute, and the checkbox
  // has to show what is stored.
  const [enabled, setEnabled] = React.useState(settings.enabled);
  const [closesAtLocal, setClosesAtLocal] = React.useState(() => toEasternLocal(settings.sheet_closes_at));
  const [startAtLocal, setStartAtLocal] = React.useState(() => toEasternLocal(settings.competition_start_at));
  const [endAtLocal, setEndAtLocal] = React.useState(() => toEasternLocal(settings.competition_end_at));
  const [compSlots, setCompSlots] = React.useState(String(settings.comp_slots));
  const [potFrontDollars, setPotFrontDollars] = React.useState(String(settings.pot_front_cents / 100));
  const [potPerEntrantDollars, setPotPerEntrantDollars] = React.useState(String(settings.pot_per_entrant_cents / 100));
  const [potPerSeatDollars, setPotPerSeatDollars] = React.useState(String(settings.pot_per_seat_cents / 100));
  const [referralJump, setReferralJump] = React.useState(String(settings.referral_jump));

  const [saving, setSaving] = React.useState(false);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [notice, setNotice] = React.useState<string | null>(null);

  const [couponBusy, setCouponBusy] = React.useState(false);
  const [couponError, setCouponError] = React.useState<string | null>(null);

  async function doSave() {
    setSaving(true);
    setErrors({});
    setNotice(null);
    const payload: LaunchSettingsInput = {
      enabled,
      sheet_closes_at: closesAtLocal ? fromEasternLocal(closesAtLocal) : null,
      competition_start_at: startAtLocal ? fromEasternLocal(startAtLocal) : null,
      competition_end_at: endAtLocal ? fromEasternLocal(endAtLocal) : null,
      // Every one of the four is a whole number server-side — round here so a
      // typed "1.5" is a 2, not a 422 (the pot fields already did).
      comp_slots: Math.round(Number(compSlots)),
      pot_front_cents: Math.round(Number(potFrontDollars) * 100),
      pot_per_entrant_cents: Math.round(Number(potPerEntrantDollars) * 100),
      pot_per_seat_cents: Math.round(Number(potPerSeatDollars) * 100),
      referral_jump: Math.round(Number(referralJump)),
    };
    const result = await saveLaunchSettings(payload);
    setSaving(false);
    if (!result.ok) {
      setErrors(fieldErrorsFrom(result) ?? { _form: describeError(result) });
      return;
    }
    setNotice("Saved.");
    router.refresh();
  }

  // A save that flips `enabled` or moves `sheet_closes_at` to the other side
  // of "now" changes what every signed-in user's next poll sees — gate those
  // behind the typed-launch confirm, not the ones that just tweak pot math.
  // Captured once (not read directly in render — Date.now() is impure), same
  // pattern as settings/page.tsx / ChatLayout.tsx / WorkspaceSidebar.tsx.
  const [now] = React.useState(() => Date.now());
  const oldCloseMs = settings.sheet_closes_at ? Date.parse(settings.sheet_closes_at) : null;
  const newCloseIso = closesAtLocal ? fromEasternLocal(closesAtLocal) : null;
  const newCloseMs = newCloseIso ? Date.parse(newCloseIso) : null;
  const closeCrossesNow = oldCloseMs !== null && newCloseMs !== null && oldCloseMs > now !== newCloseMs > now;
  const needsConfirm = enabled !== settings.enabled || closeCrossesNow;

  async function doMintCoupon(force: boolean) {
    setCouponBusy(true);
    setCouponError(null);
    setNotice(null);
    const result = await createLaunchStripeCoupon(force);
    setCouponBusy(false);
    if (!result.ok) {
      setCouponError(describeError(result));
      return;
    }
    setNotice("Stripe coupon created.");
    router.refresh();
  }

  const couponExists = !!settings.stripe_coupon_id;

  return (
    <Card title="Launch settings">
      <div className="space-y-4">
        {errors._form ? <ErrorBanner error={errors._form} /> : null}
        {notice ? <ErrorBanner error={notice} variant="info" /> : null}

        <div>
          <div className="flex items-center gap-2">
            <Checkbox aria-label="Enabled" checked={enabled} onCheckedChange={(v) => setEnabled(v === true)} />
            <span className="text-sm text-foreground">Enabled</span>
          </div>
          {errors.enabled ? <p className="text-2xs text-err">{errors.enabled}</p> : null}
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="space-y-1">
            <label
              htmlFor="launch-sheet-closes-at"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Sheet closes (ET)
            </label>
            <Input
              id="launch-sheet-closes-at"
              type="datetime-local"
              value={closesAtLocal}
              onChange={(e) => setClosesAtLocal(e.target.value)}
            />
            {errors.sheet_closes_at ? <p className="text-2xs text-err">{errors.sheet_closes_at}</p> : null}
          </div>
          <div className="space-y-1">
            <label
              htmlFor="launch-start-at"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Competition start (ET)
            </label>
            <Input
              id="launch-start-at"
              type="datetime-local"
              value={startAtLocal}
              onChange={(e) => setStartAtLocal(e.target.value)}
            />
            {errors.competition_start_at ? <p className="text-2xs text-err">{errors.competition_start_at}</p> : null}
          </div>
          <div className="space-y-1">
            <label
              htmlFor="launch-end-at"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Competition end (ET)
            </label>
            <Input
              id="launch-end-at"
              type="datetime-local"
              value={endAtLocal}
              onChange={(e) => setEndAtLocal(e.target.value)}
            />
            {errors.competition_end_at ? <p className="text-2xs text-err">{errors.competition_end_at}</p> : null}
          </div>
        </div>
        <p className="text-2xs text-muted-foreground">
          Joins are open until the sheet closes; the board runs start → end.
        </p>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <div className="space-y-1">
            <label
              htmlFor="launch-comp-slots"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Comp slots
            </label>
            <Input
              id="launch-comp-slots"
              type="number"
              min={1}
              step={1}
              value={compSlots}
              onChange={(e) => setCompSlots(e.target.value)}
            />
            {errors.comp_slots ? <p className="text-2xs text-err">{errors.comp_slots}</p> : null}
          </div>
          <div className="space-y-1">
            <label
              htmlFor="launch-referral-jump"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Spots per referral
            </label>
            <Input
              id="launch-referral-jump"
              type="number"
              min={0}
              step={1}
              value={referralJump}
              onChange={(e) => setReferralJump(e.target.value)}
            />
            {errors.referral_jump ? <p className="text-2xs text-err">{errors.referral_jump}</p> : null}
          </div>
          <div className="space-y-1">
            <label
              htmlFor="launch-pot-front"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Pot front ($)
            </label>
            <Input
              id="launch-pot-front"
              type="number"
              min={0}
              step={1}
              value={potFrontDollars}
              onChange={(e) => setPotFrontDollars(e.target.value)}
            />
            {errors.pot_front_cents ? <p className="text-2xs text-err">{errors.pot_front_cents}</p> : null}
          </div>
          <div className="space-y-1">
            <label
              htmlFor="launch-pot-per-entrant"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Pot per entrant ($)
            </label>
            <Input
              id="launch-pot-per-entrant"
              type="number"
              min={0}
              step={1}
              value={potPerEntrantDollars}
              onChange={(e) => setPotPerEntrantDollars(e.target.value)}
            />
            {errors.pot_per_entrant_cents ? <p className="text-2xs text-err">{errors.pot_per_entrant_cents}</p> : null}
          </div>
          <div className="space-y-1">
            <label
              htmlFor="launch-pot-per-seat"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Pot per team seat ($)
            </label>
            <Input
              id="launch-pot-per-seat"
              type="number"
              min={0}
              step={1}
              value={potPerSeatDollars}
              onChange={(e) => setPotPerSeatDollars(e.target.value)}
            />
            {errors.pot_per_seat_cents ? <p className="text-2xs text-err">{errors.pot_per_seat_cents}</p> : null}
          </div>
        </div>
        <p className="text-2xs text-muted-foreground">
          The pot fields are internal — never shown publicly. An individual entrant contributes the per-entrant
          amount; a counted team contributes the per-entrant amount plus the per-seat amount for every seat.
        </p>

        {needsConfirm ? (
          <ConfirmActionDialog
            confirmText="launch"
            actionLabel="Change what every signed-in user sees within 30 seconds"
            onConfirm={doSave}
          >
            <Button type="button" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </ConfirmActionDialog>
        ) : (
          <Button type="button" onClick={doSave} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        )}

        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">Stripe coupon</h3>
          <p className="text-2xs text-muted-foreground">
            100% off once, no promo codes — it&apos;s attached server-side to winners, never typed at checkout.
          </p>
          <DefRow label="Coupon id" value={settings.stripe_coupon_id || "—"} mono />
          {couponError ? <ErrorBanner error={couponError} /> : null}
          {couponExists ? (
            <ConfirmActionDialog
              confirmText="create"
              actionLabel="Re-mint the Stripe coupon"
              onConfirm={() => doMintCoupon(true)}
            >
              <Button type="button" variant="outline" size="sm" disabled={couponBusy}>
                {couponBusy ? "Working…" : "Create Stripe coupon"}
              </Button>
            </ConfirmActionDialog>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={couponBusy}
              onClick={() => doMintCoupon(false)}
            >
              {couponBusy ? "Working…" : "Create Stripe coupon"}
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
