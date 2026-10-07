"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { RefreshCw, Wallet } from "lucide-react";
import { useCredits } from "@/hooks/useCredits";

const CARD = "rounded-lg border border-[var(--edge)] bg-cream p-4 space-y-3";
const EYEBROW = "label-caps";
const QUICK_PICKS_CENTS = [1000, 2000, 5000, 10000];

/** Workspace-managed AI credits, purchased through a Metronome-gated commit. */
export function CreditsSection() {
  const { scopeKey, balance, refresh, startTopUp, openBilling, canManageCredits, pendingTopUp, topUpOutcome } = useCredits();
  const [amount, setAmount] = useState(2000);
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState<number | null>(null);
  const [lockedAmount, setLockedAmount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const scopeGeneration = useRef(0);

  useLayoutEffect(() => {
    scopeGeneration.current += 1;
    setAmount(2000);
    setSubmitting(false);
    setRequested(null);
    setLockedAmount(null);
    setError(null);
    return () => { scopeGeneration.current += 1; };
  }, [scopeKey]);

  useEffect(() => {
    if (pendingTopUp) {
      setAmount(pendingTopUp.amount_cents);
      setLockedAmount(pendingTopUp.amount_cents);
      setRequested(pendingTopUp.state === "pending" ? null : pendingTopUp.amount_cents);
    } else {
      setLockedAmount(null);
      setRequested(null);
    }
  }, [pendingTopUp, scopeKey]);

  const topUp = async () => {
    const requestScope = scopeGeneration.current;
    const intentAmount = lockedAmount ?? amount;
    if (lockedAmount === null) setLockedAmount(intentAmount);
    setSubmitting(true);
    setError(null);
    try {
      const result = await startTopUp(intentAmount);
      if (requestScope !== scopeGeneration.current) return;
      if (result.status !== "requested") throw new Error("Top-up was not accepted");
      // Preserve both the amount and useCredits' UUID through uncertain
      // collection; a second click cannot create a different charge.
      setRequested(intentAmount);
      void refresh();
    } catch (cause) {
      if (requestScope === scopeGeneration.current) {
        setError(cause instanceof Error ? cause.message : "Couldn't start top-up");
      }
    } finally {
      if (requestScope === scopeGeneration.current) setSubmitting(false);
    }
  };

  const display = balance?.balance_dollars === null || !balance ? "—" : `$${balance.balance_dollars}`;
  return (
    <div className="space-y-6">
      <div className={CARD}>
        <div className="flex items-center justify-between"><div className="flex items-center gap-2"><Wallet size={16} className="text-[var(--midnight-muted)]" /><span className={EYEBROW}>BALANCE</span></div>
          <button type="button" onClick={() => refresh()} aria-label="Refresh" className="text-[var(--midnight-muted)] hover:text-[var(--midnight)]"><RefreshCw size={16} /></button></div>
        <div className="text-3xl font-semibold font-mono text-[var(--midnight)]">{display}</div>
        {balance?.balance_dollars === null && <p className="text-xs text-[var(--midnight-muted)]">Balance unavailable right now — try refreshing in a moment.</p>}
      </div>
      <div className={CARD}>
        <span className={EYEBROW}>ADD CREDITS</span>
        {!canManageCredits && <p className="text-sm text-[var(--midnight-muted)]">Only workspace admins can add credits.</p>}
        {canManageCredits && <>
        <div className="flex flex-wrap gap-2">{QUICK_PICKS_CENTS.map((cents) => <button key={cents} disabled={lockedAmount !== null} onClick={() => setAmount(cents)} className={"rounded-md border px-3 py-1.5 text-sm transition-colors " + (amount === cents ? "border-[var(--midnight)] bg-[var(--cream-deep)] text-[var(--midnight)]" : "border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)]")}>${cents / 100}</button>)}</div>
        {error && <p className="text-sm text-err">{error}</p>}
        {topUpOutcome === "failed" && <p className="text-sm text-err">Payment failed. Choose an amount to try again.</p>}
        {pendingTopUp?.state === "action_required"
          ? <p className="text-sm text-err">Payment needs attention. Update your payment method in billing.</p>
          : requested !== null && <p className="text-sm text-ok">Top-up of ${requested / 100} requested — credits can take a few minutes to land.</p>}
        {pendingTopUp?.state === "action_required" && <button type="button" onClick={() => void openBilling()} className="text-sm font-medium underline text-[var(--midnight)]">Open billing</button>}
        <button onClick={topUp} disabled={submitting || requested !== null || amount < 500} className="rounded-full bg-[var(--midnight)] hover:opacity-90 text-cream px-4 py-2 text-sm disabled:opacity-50">
          {submitting ? "Loading…" : requested !== null ? "Top-up requested" : `${lockedAmount !== null ? "Retry" : "Add"} $${amount / 100}`}
        </button>
        </>}
        <p className="text-xs text-[var(--midnight-muted)]">Metronome charges the workspace&apos;s saved payment method and releases credits after collection.</p>
      </div>
    </div>
  );
}
