"use client";

import * as React from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { LAUNCH_REF_KEY, formatMoney, type LaunchInfo, type LaunchMe } from "@/lib/launch";
import { ConnectStripeForm } from "./ConnectStripeForm";
import { JoinForm } from "./JoinForm";
import { LaunchStatusLines } from "./LaunchStatusLines";
import { useLaunchRef } from "./useLaunchRef";

const BLOCKED_COPY: Record<string, string> = {
  no_subscription: "Redeem your code or subscribe to join — the free month is the competition month.",
  phase_ended: "The competition has ended.",
};

export function MyLaunchCard({ info }: { info: LaunchInfo }) {
  const api = useApi();
  const refCode = useLaunchRef();
  const { data: me, mutate } = useSWR<LaunchMe>("/launch/me", (k: string) => api.get(k) as Promise<LaunchMe>);
  const attributed = React.useRef(false);
  const [publicBusy, setPublicBusy] = React.useState(false);
  const [publicError, setPublicError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!me || !refCode || attributed.current) return;
    if (me.waitlist && me.waitlist.referral_code === refCode) return;
    attributed.current = true;
    // Only clear the stored ref once we get a resolved response (attributed
    // true OR false) — a thrown error (network blip, 5xx) must leave it in
    // place so the once-guard's next mount can retry.
    api
      .post("/launch/referral", { ref: refCode })
      .then(() => {
        try {
          window.localStorage.removeItem(LAUNCH_REF_KEY);
        } catch {
          /* storage unavailable */
        }
      })
      .catch(() => undefined);
  }, [me, refCode, api]);

  const togglePublic = async (checked: boolean) => {
    setPublicBusy(true);
    setPublicError(null);
    try {
      await api.patch("/launch/entry", { public: checked });
      await mutate();
    } catch {
      setPublicError("Couldn't update that — try again.");
    } finally {
      setPublicBusy(false);
    }
  };

  if (!me) return <div className="lc-card"><p className="lc-fine">Loading your status…</p></div>;

  return (
    <div className="lc-card" data-testid="my-launch-card">
      <p className="ld-kicker">Your launch</p>

      {me.phase === "waitlist" ? (
        <LaunchStatusLines me={me} dropAt={info.drop_at} />
      ) : !me.activated ? (
        <p className="lc-fine">Finish onboarding — Friday&apos;s first run has to complete.</p>
      ) : !me.joined ? (
        me.can_join ? (
          <JoinForm onJoined={() => mutate()} />
        ) : (
          <p className="lc-fine">{BLOCKED_COPY[me.reason ?? ""] ?? "You can't join right now."}</p>
        )
      ) : (
        <div>
          {me.entry ? <p className="ld-lede">You&apos;re in as {me.entry.display_name}.</p> : null}
          <label className="lc-check">
            <input
              type="checkbox"
              checked={me.entry?.public ?? true}
              disabled={publicBusy}
              onChange={(e) => togglePublic(e.target.checked)}
            />
            Show my name on the board
          </label>
          {publicError ? <p role="alert" className="lc-alert">{publicError}</p> : null}
          {me.entry?.refresh_error ? (
            <div style={{ marginTop: 16 }}>
              <p className="lc-alert">Couldn&apos;t refresh ({me.entry.refresh_error}) — reconnect with a fresh key.</p>
              <ConnectStripeForm onConnected={() => mutate()} />
            </div>
          ) : !me.has_key ? (
            <div style={{ marginTop: 16 }}>
              <p className="lc-fine">You&apos;re in. Connect a read-only Stripe key to get on the board.</p>
              <ConnectStripeForm onConnected={() => mutate()} />
            </div>
          ) : (
            <p className="lc-fine" style={{ marginTop: 16 }}>
              Lift so far: <span style={{ fontVariantNumeric: "tabular-nums", color: "var(--ink)" }}>{formatMoney(me.entry?.delta_cents ?? 0, me.entry?.currency ?? "usd")}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
