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

/** Save one launch email's copy. Blank fields are dropped server-side and
 * fall back to the shipped default, so sending all three blank is the reset.
 * 422 returns `data: {detail: {errors: {field: message}}}` — same envelope as
 * the settings PUT — for an unsupported placeholder. */
export async function saveLaunchEmailCopy(body: {
  variant: string;
  subject: string;
  lead: string;
  detail: string;
}): Promise<ActionResult> {
  return adminPut("/ops/launch/email-copy", body);
}

/** Send one variant, with sample values, to the signed-in admin's own Clerk
 * address. Touches no sheet row and stamps no email claim. */
export async function sendLaunchTestEmail(variant: string): Promise<ActionResult> {
  return adminPost("/ops/launch/email-test", { variant });
}

/** Correct a team submission's seat count, and decide whether it counts
 * toward the pot. Only a COUNTED entry contributes — an unreviewed submission
 * never inflates the pot. 422 returns `data: {detail: {errors: {field: msg}}}`,
 * the same envelope as the settings PUT. */
export async function setLaunchTeamEntry(
  ownerId: string,
  body: { seats?: number; counted?: boolean },
): Promise<ActionResult> {
  return adminPost(`/ops/launch/team-entries/${encodeURIComponent(ownerId)}`, body);
}
