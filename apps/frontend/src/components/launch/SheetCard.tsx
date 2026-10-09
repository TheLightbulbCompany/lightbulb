"use client";

import * as React from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { formatMoney, type LaunchInfo, type LaunchMe, type LaunchMeEntry } from "@/lib/launch";
import { ConnectStripeForm } from "./ConnectStripeForm";
import { ReferralLink } from "./ReferralLink";
import { RenameForm } from "./RenameForm";
import { useLaunchRef } from "./useLaunchRef";
import { useReferralAttribution } from "./useReferralAttribution";

/**
 * The signed-in "Your launch" card on /launch — the caller's competition-sheet
 * membership. States: not joined (open phase: the team hand-raise) → sheet position +
 * referrals; post-close it carries the winner / sheet-closed outcome, and
 * during the competition the revenue-board attachment (Stripe connect +
 * public toggle).
 *
 * Board state comes from `me.entry` (the /launch/me projection) and is
 * updated in-session by POST /launch/connect-stripe's fuller response; the
 * public toggle appears once an entry is in hand.
 */
export function SheetCard({ info }: { info: LaunchInfo }) {
  const api = useApi();
  useLaunchRef(); // side effect: persists `?ref=` on `/launch?ref=CODE` too
  const { data: me, mutate } = useSWR<LaunchMe>("/launch/me", (k: string) => api.get(k) as Promise<LaunchMe>);
  useReferralAttribution(api, me);
  const [entry, setEntry] = React.useState<LaunchMeEntry | null>(null);
  const [publicBusy, setPublicBusy] = React.useState(false);
  const [publicError, setPublicError] = React.useState<string | null>(null);
  // An in-session connect/toggle wins; otherwise /launch/me's projection.
  const boardEntry = entry ?? me?.entry ?? null;

  const togglePublic = async (checked: boolean) => {
    setPublicBusy(true);
    setPublicError(null);
    try {
      await api.patch("/launch/entry", { public: checked });
      if (boardEntry) setEntry({ ...boardEntry, public: checked });
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
        {open ? (
          // No self-serve join: `POST /launch/join` refuses every workspace
          // (org) context — the sheet keys personal while billing keys the
          // org — and every account is a workspace now. A join form here
          // could only fail, so point at the team hand-raise instead.
          <p className="lc-fine">
            Workspaces enter by hand — <a href="#teams">raise your hand as a team</a> and the founders add you.
          </p>
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

      {/* Renameable for as long as the backend allows it (joined, eligible,
          competition not over) — NOT only while the sheet is open. Owners now
          join during provisioning, before Friday has named their company, and
          the public board does not go live until the `competition` phase,
          which is exactly when someone is most likely to want their name
          fixed. Gating this on `open` would close the door right as the board
          opens. `rename_display_name` refuses the `ended` phase itself. */}
      {info.phase !== "ended" && me.compete_eligible ? (
        <RenameForm
          current={me.display_name}
          onRenamed={(display_name) => mutate({ ...me, display_name }, { revalidate: false })}
        />
      ) : null}

      {info.phase === "competition" && me.compete_eligible ? (
        <div style={{ marginTop: 16 }}>
          {boardEntry ? (
            <>
              <label className="lc-check">
                <input
                  type="checkbox"
                  checked={boardEntry.public}
                  disabled={publicBusy}
                  onChange={(e) => togglePublic(e.target.checked)}
                />
                Show my name on the board
              </label>
              {publicError ? <p role="alert" className="lc-alert">{publicError}</p> : null}
            </>
          ) : null}
          {boardEntry?.refresh_error ? (
            <div style={{ marginTop: 16 }}>
              <p className="lc-alert">Couldn&apos;t refresh ({boardEntry.refresh_error}) — reconnect with a fresh key.</p>
              <ConnectStripeForm onConnected={setEntry} />
            </div>
          ) : boardEntry?.has_key ? (
            <p className="lc-fine" style={{ marginTop: 16 }}>
              Lift so far: <span style={{ fontVariantNumeric: "tabular-nums", color: "var(--midnight)" }}>{formatMoney(boardEntry.delta_cents ?? 0, boardEntry.currency ?? "usd")}</span>
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
