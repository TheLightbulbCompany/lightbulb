"use client";

import * as React from "react";
import Link from "next/link";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { formatMoney, type LaunchEntry, type LaunchInfo, type LaunchMe } from "@/lib/launch";
import { ConnectStripeForm } from "./ConnectStripeForm";
import { JoinForm } from "./JoinForm";
import { ReferralLink } from "./ReferralLink";
import { useLaunchRef } from "./useLaunchRef";
import { useReferralAttribution } from "./useReferralAttribution";

/**
 * The signed-in "Your launch" card on /launch — the caller's competition-sheet
 * membership. States: onboard first → join (open phase) → sheet position +
 * referrals; post-close it carries the winner / sheet-closed outcome, and
 * during the competition the revenue-board attachment (Stripe connect +
 * public toggle).
 *
 * The board attachment (`LaunchEntry`) has no read route — POST
 * /launch/connect-stripe's response is its only surface — so board state is
 * session-local: the connect form always renders until a connect succeeds
 * (reconnecting is an idempotent key replace), and the public toggle appears
 * once an entry is in hand.
 */
export function SheetCard({ info }: { info: LaunchInfo }) {
  const api = useApi();
  useLaunchRef(); // side effect: persists `?ref=` on `/launch?ref=CODE` too
  const { data: me, mutate } = useSWR<LaunchMe>("/launch/me", (k: string) => api.get(k) as Promise<LaunchMe>);
  useReferralAttribution(api, me);
  const [entry, setEntry] = React.useState<LaunchEntry | null>(null);
  const [publicBusy, setPublicBusy] = React.useState(false);
  const [publicError, setPublicError] = React.useState<string | null>(null);

  const togglePublic = async (checked: boolean) => {
    setPublicBusy(true);
    setPublicError(null);
    try {
      await api.patch("/launch/entry", { public: checked });
      setEntry((e) => (e ? { ...e, public: checked } : e));
    } catch {
      setPublicError("Couldn't update that — try again.");
    } finally {
      setPublicBusy(false);
    }
  };

  if (!me) return <div className="lc-card"><p className="lc-fine">Loading your status…</p></div>;

  const open = info.phase === "open";

  if (!me.joined) {
    return (
      <div className="lc-card" data-testid="sheet-card">
        <p className="ld-kicker">Your launch</p>
        {!me.onboarded ? (
          <div>
            <p className="lc-fine">Onboard first — Friday&apos;s first run has to complete. Then come back and join.</p>
            <div className="lc-actions">
              <Link href="/chat" className="ld-btn">Onboard first <span className="ld-btn-arrow" aria-hidden="true">→</span></Link>
            </div>
          </div>
        ) : open ? (
          <JoinForm onJoined={() => mutate()} />
        ) : (
          <p className="lc-fine">The sheet has closed — you can&apos;t join the competition anymore, and nothing else changes.</p>
        )}
      </div>
    );
  }

  return (
    <div className="lc-card" data-testid="sheet-card">
      <p className="ld-kicker">Your launch</p>

      {me.winner ? (
        <div>
          <p className="ld-lede">You won a free month.</p>
          <p className="lc-fine">
            It&apos;s applied automatically — at checkout if you haven&apos;t subscribed yet, or on your next invoice if you have.
          </p>
        </div>
      ) : !me.compete_eligible ? (
        <p className="lc-fine">
          The sheet closed — subscribe to keep building. You&apos;re not in the competition; everything else works exactly the same.
        </p>
      ) : null}

      {me.effective_position !== null ? (
        <p className="ld-lede">
          You&apos;re #{me.effective_position}
          {me.position !== null && me.position !== me.effective_position ? (
            <span className="lc-fine"> (#{me.position} before referral jumps)</span>
          ) : null}
        </p>
      ) : null}

      {open ? (
        <div>
          <p className="lc-fine">
            {me.referrals_subscribed} subscribed referral{me.referrals_subscribed === 1 ? "" : "s"} — each one moves
            you up {info.referral_jump} spots (the first 20 count).
          </p>
          <p className="lc-fine">
            Referral credits: 5 subscribed referrals earn $50 · 20 earn $250 total.
            {me.credits_milestone_reached > 0
              ? ` You've earned ${me.credits_milestone_reached === 20 ? "$250" : "$50"} in credits.`
              : ""}
          </p>
          {me.referral_code && me.referral_link ? (
            <div style={{ marginTop: 16 }}>
              <ReferralLink code={me.referral_code} link={me.referral_link} />
            </div>
          ) : null}
        </div>
      ) : null}

      {info.phase === "competition" && me.compete_eligible ? (
        <div style={{ marginTop: 16 }}>
          {entry ? (
            <>
              <label className="lc-check">
                <input
                  type="checkbox"
                  checked={entry.public}
                  disabled={publicBusy}
                  onChange={(e) => togglePublic(e.target.checked)}
                />
                Show my name on the board
              </label>
              {publicError ? <p role="alert" className="lc-alert">{publicError}</p> : null}
            </>
          ) : null}
          {entry?.refresh_error ? (
            <div style={{ marginTop: 16 }}>
              <p className="lc-alert">Couldn&apos;t refresh ({entry.refresh_error}) — reconnect with a fresh key.</p>
              <ConnectStripeForm onConnected={setEntry} />
            </div>
          ) : entry?.has_key ? (
            <p className="lc-fine" style={{ marginTop: 16 }}>
              Lift so far: <span style={{ fontVariantNumeric: "tabular-nums", color: "var(--midnight)" }}>{formatMoney(entry.delta_cents, entry.currency ?? "usd")}</span>
            </p>
          ) : (
            <div>
              <p className="lc-fine">Connect a read-only Stripe key to get on the board. Reconnecting replaces the stored key.</p>
              <ConnectStripeForm onConnected={setEntry} />
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
