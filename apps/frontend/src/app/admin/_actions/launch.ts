"use server";

import { adminPost, type ActionResult } from "./_shared";

export async function refreshLaunch(): Promise<ActionResult> {
  return adminPost("/ops/launch/refresh");
}
