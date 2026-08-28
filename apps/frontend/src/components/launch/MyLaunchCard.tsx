"use client";

import * as React from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { LAUNCH_REF_KEY, formatMoney, type LaunchInfo, type LaunchMe } from "@/lib/launch";
import { EnterForm } from "./EnterForm";
import { ReferralLink } from "./ReferralLink";
import { useLaunchRef } from "./useLaunchRef";

const BLOCKED_COPY: Record<string, string> = {
  phase_waitlist: "Entries open on drop day.",
  phase_ended: "The competition has ended.",
  no_subscription: "Subscribe (or redeem your code at checkout) to enter.",
  not_activated: "Finish onboarding — Friday's first run has to complete — and come back.",
};

export function MyLaunchCard({ info }: { info: LaunchInfo }) {
  const api = useApi();
  const refCode = useLaunchRef();
  const { data: me, mutate } = useSWR<LaunchMe>("/launch/me", (k: string) => api.get(k) as Promise<LaunchMe>);
  const attributed = React.useRef(false);

  React.useEffect(() => {
    if (!me || !refCode || attributed.current) return;
    if (me.waitlist && me.waitlist.referral_code === refCode) return;
    attributed.current = true;
    api
      .post("/launch/referral", { ref: refCode })
      .catch(() => undefined)
      .finally(() => {
        try {
          window.localStorage.removeItem(LAUNCH_REF_KEY);
        } catch {
          /* storage unavailable */
        }
      });
  }, [me, refCode, api]);

  if (!me) return <div className="lc-card"><p className="lc-fine">Loading your status…</p></div>;

  return (
    <div className="lc-card" data-testid="my-launch-card">
      <p className="ld-kicker">Your launch</p>
      {me.waitlist ? (
        <p className="ld-lede">
          Waitlist #{me.waitlist.position}
          {me.waitlist.in_comp_window ? ` — in the first ${info.comp_slots}. Check your inbox for your code on drop day.` : "."}
        </p>
      ) : null}
      <p className="lc-fine">{me.activated ? "Onboarding complete ✓" : "Onboarding not finished yet."}</p>

      {me.waitlist ? (
        <div style={{ marginTop: 16 }}>
          <p className="lc-fine">
            <span>{me.waitlist.referrals_signed_up} signed up</span> · <span>{me.waitlist.referrals_paid} paid</span> ·{" "}
            <span>{me.waitlist.free_months_banked} free month{me.waitlist.free_months_banked === 1 ? "" : "s"} banked</span>
          </p>
          <ReferralLink code={me.waitlist.referral_code} link={me.waitlist.referral_link} />
        </div>
      ) : null}

      <div style={{ marginTop: 24, paddingTop: 16, borderTop: "1px solid var(--edge)" }}>
        {me.entry ? (
          <div>
            <p className="ld-lede">You&apos;re in as {me.entry.display_name}.</p>
            <p className="lc-fine">
              Lift so far: <span style={{ fontVariantNumeric: "tabular-nums", color: "var(--ink)" }}>{formatMoney(me.entry.delta_cents, me.entry.currency ?? "usd")}</span>
              {me.entry.refresh_error ? <span className="lc-alert" style={{ display: "inline", marginLeft: 8 }}>— couldn&apos;t refresh ({me.entry.refresh_error}); re-enter with a fresh key.</span> : null}
            </p>
            {me.entry.refresh_error ? <div style={{ marginTop: 16 }}><EnterForm onEntered={() => mutate()} /></div> : null}
          </div>
        ) : me.can_enter ? (
          <EnterForm onEntered={() => mutate()} />
        ) : (
          <p className="lc-fine">{BLOCKED_COPY[me.reason ?? ""] ?? "You can't enter right now."}</p>
        )}
      </div>
    </div>
  );
}
