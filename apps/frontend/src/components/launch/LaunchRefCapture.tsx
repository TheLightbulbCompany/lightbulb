"use client";

import { useLaunchRef } from "./useLaunchRef";

/**
 * Invisible. `launch_service.referral_link()` points at the landing ROOT
 * (`/?ref=CODE`), so something on `/` has to persist the code — this is it.
 * `useReferralAttribution` picks it up from localStorage after signup.
 *
 * Renders null and uses `useSearchParams`, so mount it inside
 * `<Suspense fallback={null}>`.
 */
export function LaunchRefCapture() {
  useLaunchRef();
  return null;
}
