"use server";

import type { LaunchSettingsInput } from "../_lib/api";
import { adminDelete, adminPost, adminPut, type ActionResult } from "./_shared";

export async function refreshLaunch(): Promise<ActionResult> {
  return adminPost("/ops/launch/refresh");
}

/** Take an entry off the public board (and revoke its stored Stripe key). */
export async function removeLaunchEntry(ownerId: string): Promise<ActionResult> {
  return adminDelete(`/ops/launch/entries/${encodeURIComponent(ownerId)}`);
}

/** Full-replace save of the launch-settings row. 422 on bad input returns
 * `data: {detail: {errors: {field: message}}}` — the coupon fields are
 * server-managed and never sent here (`PUT` preserves them regardless). */
export async function saveLaunchSettings(body: LaunchSettingsInput): Promise<ActionResult> {
  return adminPut("/ops/launch/settings", body);
}

/** One-click mint of the free-month Stripe coupon (100% once, no promo
 * codes — it's attached server-side, never typed). 409 when one is already
 * configured and `force` wasn't passed, 422/502 otherwise. */
export async function createLaunchStripeCoupon(force: boolean): Promise<ActionResult> {
  return adminPost("/ops/launch/settings/stripe-coupon", { force });
}

/** Close the competition sign-up sheet: freeze joins/jumps, flag winners,
 * freeze the pot, attach coupons, email winners. 409 "already_closed"
 * without `force`; a forced rerun retries only the failed coupon/email
 * steps (convergence, never a re-flag). */
export async function closeLaunchSheet(force: boolean): Promise<ActionResult> {
  return adminPost("/ops/launch/close", { force });
}
