"use client";

import * as React from "react";
import type { LaunchMe } from "@/lib/launch";
import { ReferralLink } from "./ReferralLink";
import { useCountdown } from "./useCountdown";

/**
 * Pre-drop status: waitlist position, countdown to the drop, the idea
 * teaser, and the referral link. Shared between the in-app held card
 * (`components/chat/LaunchHeldCard`) and `/launch`'s `MyLaunchCard` before
 * doors open — same account, same facts, two surfaces.
 */
export function LaunchStatusLines({ me, dropAt }: { me: LaunchMe; dropAt: string }) {
  const countdown = useCountdown(dropAt) ?? "—";
  return (
    <div>
      {me.waitlist ? (
        <>
          <p className="ld-lede">You&apos;re #{me.waitlist.effective_position} on the list.</p>
          {me.waitlist.moved_up > 0 ? (
            <p className="lc-fine">
              Up {me.waitlist.moved_up} spots from {me.waitlist.referrals_signed_up} referrals.
            </p>
          ) : null}
          <p className="lc-fine">Each referral moves you up {me.waitlist.referral_jump} spots.</p>
        </>
      ) : null}
      <p className="lc-fine">{countdown} until the drop</p>
      {me.idea ? <p className="lc-fine">Your idea: {me.idea}</p> : <p className="lc-fine">Add your idea on drop day.</p>}
      {me.waitlist ? (
        <div style={{ marginTop: 16 }}>
          <ReferralLink code={me.waitlist.referral_code} link={me.waitlist.referral_link} />
        </div>
      ) : null}
    </div>
  );
}
