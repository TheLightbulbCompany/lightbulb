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

/** One-click mint of the free-month Stripe coupon + promotion code. 409 when
 * one is already configured and `force` wasn't passed, 422 when the dates
 * aren't set yet, 502 on a Stripe-side failure. */
export async function createLaunchStripeCoupon(force: boolean): Promise<ActionResult> {
  return adminPost("/ops/launch/settings/stripe-coupon", { force });
}
