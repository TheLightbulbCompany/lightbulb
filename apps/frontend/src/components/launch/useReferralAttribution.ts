"use client";

import * as React from "react";
import type { useApi } from "@/lib/api";
import { LAUNCH_REF_KEY, type LaunchMe } from "@/lib/launch";

/**
 * Attributes a stored referral code once the caller's own `/launch/me` row is
 * known. Shared by `/launch`'s MyLaunchCard and the landing hero's
 * LaunchSpotCard, so a held user gets attributed without ever visiting
 * `/launch`.
 *
 * The code is read from localStorage (written by `useLaunchRef`, mounted on
 * the landing root via LaunchRefCapture and on `/launch` itself) and removed
 * ONLY on a resolved response — attributed true OR false. A thrown error
 * (network blip, 5xx) leaves it in place so the once-guard's next mount can
 * retry.
 */
export function useReferralAttribution(api: ReturnType<typeof useApi>, me: LaunchMe | null | undefined): void {
  const attributed = React.useRef(false);
  React.useEffect(() => {
    if (!me || attributed.current) return;
    let ref: string | null = null;
    try {
      ref = window.localStorage.getItem(LAUNCH_REF_KEY);
    } catch {
      return; /* storage unavailable */
    }
    if (!ref) return;
    if (me.waitlist?.referral_code === ref) return; // never self-attribute
    attributed.current = true;
    api
      .post("/launch/referral", { ref })
      .then(() => {
        try {
          window.localStorage.removeItem(LAUNCH_REF_KEY);
        } catch {
          /* storage unavailable */
        }
      })
      .catch(() => undefined);
  }, [api, me]);
}
