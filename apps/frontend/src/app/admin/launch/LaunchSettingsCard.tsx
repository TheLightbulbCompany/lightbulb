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
 * The admin editor for the launch-settings DynamoDB row — dates, comp slots,
 * pot constants, and the one-click Stripe coupon mint. Always rendered
 * (`app/admin/launch/page.tsx` mounts it regardless of `enabled`, since an
 * admin has to be able to turn the feature ON from here); the rest of the
 * economics sheet only shows up once the row is enabled and computes clean.
 */
export function LaunchSettingsCard({ settings }: LaunchSettingsCardProps) {
  const router = useRouter();

  // The saved row itself, never the sheet's derived `enabled` — those differ
  // whenever the row is on but the sheet failed to compute, and the checkbox
  // has to show what is stored.
  const [enabled, setEnabled] = React.useState(settings.enabled);
  const [dropAtLocal, setDropAtLocal] = React.useState(() => toEasternLocal(settings.drop_at));
  const [startAtLocal, setStartAtLocal] = React.useState(() => toEasternLocal(settings.competition_start_at));
  const [compSlots, setCompSlots] = React.useState(String(settings.comp_slots));
  const [competitionDays, setCompetitionDays] = React.useState(String(settings.competition_days));
  const [potFrontDollars, setPotFrontDollars] = React.useState(String(settings.pot_front_cents / 100));
  const [potPerEntrantDollars, setPotPerEntrantDollars] = React.useState(String(settings.pot_per_entrant_cents / 100));

  const [saving, setSaving] = React.useState(false);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [notice, setNotice] = React.useState<string | null>(null);

  const [couponBusy, setCouponBusy] = React.useState(false);
  const [couponError, setCouponError] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);

  async function doSave() {
    setSaving(true);
    setErrors({});
    setNotice(null);
    const payload: LaunchSettingsInput = {
      enabled,
      drop_at: dropAtLocal ? fromEasternLocal(dropAtLocal) : null,
      competition_start_at: startAtLocal ? fromEasternLocal(startAtLocal) : null,
      // Every one of the four is a whole number server-side — round here so a
      // typed "1.5" is a 2, not a 422 (the pot fields already did).
      comp_slots: Math.round(Number(compSlots)),
      competition_days: Math.round(Number(competitionDays)),
      pot_front_cents: Math.round(Number(potFrontDollars) * 100),
      pot_per_entrant_cents: Math.round(Number(potPerEntrantDollars) * 100),
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

  // A save that flips `enabled` or moves `drop_at` to the other side of "now"
  // changes what every signed-in user's next poll sees — gate those behind
  // the typed-launch confirm, not the ones that just tweak pot math. Captured
  // once (not read directly in render — Date.now() is impure), same pattern
  // as settings/page.tsx / ChatLayout.tsx / WorkspaceSidebar.tsx.
  const [now] = React.useState(() => Date.now());
  const oldDropMs = settings.drop_at ? Date.parse(settings.drop_at) : null;
  const newDropIso = dropAtLocal ? fromEasternLocal(dropAtLocal) : null;
  const newDropMs = newDropIso ? Date.parse(newDropIso) : null;
  const dropCrossesNow = oldDropMs !== null && newDropMs !== null && oldDropMs > now !== newDropMs > now;
  const needsConfirm = enabled !== settings.enabled || dropCrossesNow;

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
    setNotice("Stripe coupon + promo code created.");
    router.refresh();
  }

  async function copyPromoCode() {
    if (!settings.stripe_promotion_code) return;
    try {
      await navigator.clipboard.writeText(settings.stripe_promotion_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the code is still visible to select and copy by hand */
    }
  }

  const datesSet = !!settings.drop_at && !!settings.competition_start_at;
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

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label
              htmlFor="launch-drop-at"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Drop (ET)
            </label>
            <Input
              id="launch-drop-at"
              type="datetime-local"
              value={dropAtLocal}
              onChange={(e) => setDropAtLocal(e.target.value)}
            />
            {errors.drop_at ? <p className="text-2xs text-err">{errors.drop_at}</p> : null}
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
        </div>
        <p className="text-2xs text-muted-foreground">
          Doors open at drop; Join opens at competition start; the promo code expires at competition start.
        </p>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
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
              htmlFor="launch-competition-days"
              className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
            >
              Competition days
            </label>
            <Input
              id="launch-competition-days"
              type="number"
              min={1}
              step={1}
              value={competitionDays}
              onChange={(e) => setCompetitionDays(e.target.value)}
            />
            {errors.competition_days ? <p className="text-2xs text-err">{errors.competition_days}</p> : null}
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
        </div>

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
          <DefRow label="Coupon id" value={settings.stripe_coupon_id || "—"} mono />
          <DefRow label="Promo code id" value={settings.stripe_promotion_code_id || "—"} mono />
          <DefRow
            label="Promo code"
            value={
              settings.stripe_promotion_code ? (
                <span className="inline-flex items-center gap-2">
                  <span className="font-mono">{settings.stripe_promotion_code}</span>
                  <button
                    type="button"
                    onClick={copyPromoCode}
                    className="rounded-md border border-border px-2 py-0.5 text-2xs text-muted-foreground hover:bg-accent hover:text-foreground"
                  >
                    {copied ? "Copied" : "Copy"}
                  </button>
                </span>
              ) : (
                "—"
              )
            }
          />
          {couponError ? <ErrorBanner error={couponError} /> : null}
          {couponExists ? (
            <ConfirmActionDialog
              confirmText="create"
              actionLabel="Re-mint the Stripe coupon + promo code"
              onConfirm={() => doMintCoupon(true)}
            >
              <Button type="button" variant="outline" size="sm" disabled={!datesSet || couponBusy}>
                {couponBusy ? "Working…" : "Create Stripe coupon + promo code"}
              </Button>
            </ConfirmActionDialog>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!datesSet || couponBusy}
              onClick={() => doMintCoupon(false)}
            >
              {couponBusy ? "Working…" : "Create Stripe coupon + promo code"}
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
