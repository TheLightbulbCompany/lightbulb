"use client";

import * as React from "react";
import type { useApi } from "@/lib/api";
import { LAUNCH_REF_KEY, type LaunchMe } from "@/lib/launch";

/**
 * Attributes a stored referral code once the caller's own `/launch/me` row is
 * known (used by /launch's SheetCard and the in-chat join banner).
 *
 * The code is read from localStorage (written by `useLaunchRef`, mounted on
 * the landing root via LaunchRefCapture and on `/launch` itself) and removed
 * ONLY once the server reports `attributed: true`.
 *
 * It is deliberately NOT removed on `attributed: false`. That refusal means
 * "not this caller" — POST /launch/referral returns it for anyone who already
 * has a live subscription or whose row already carries a `referred_by` — and
 * this hook is bound to whoever happens to be signed in, not to the person the
 * link was shared with. Clearing on a refusal burned the code: an already
 * signed-in visitor clicking a friend's link POSTed it under their own
 * ineligible account, got a 200 false, and the sign-up that followed in the
 * same browser had nothing left to attribute.
 *
 * A thrown error (network blip, 5xx) also leaves it in place so the
 * once-guard's next mount can retry.
 *
 * ponytail: a browser holding a code that can never apply to it re-POSTs once
 * per mount (bounded by the route's rate limit). Distinguishing terminal
 * refusals would need a reason on the response — add it if the noise shows up.
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
    if (me.referral_code === ref) return; // never self-attribute
    attributed.current = true;
    api
      .post("/launch/referral", { ref })
      .then((res) => {
        if (!(res as { attributed?: boolean } | null)?.attributed) return;
        try {
          window.localStorage.removeItem(LAUNCH_REF_KEY);
        } catch {
          /* storage unavailable */
        }
      })
      .catch(() => undefined);
  }, [api, me]);
}
