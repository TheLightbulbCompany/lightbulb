"use server";

import { adminDelete, adminPost, type ActionResult } from "./_shared";

export async function refreshLaunch(): Promise<ActionResult> {
  return adminPost("/ops/launch/refresh");
}

/** Take an entry off the public board (and revoke its stored Stripe key). */
export async function removeLaunchEntry(ownerId: string): Promise<ActionResult> {
  return adminDelete(`/ops/launch/entries/${encodeURIComponent(ownerId)}`);
}
