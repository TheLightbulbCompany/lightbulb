"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useCredits, isCreditsExhausted } from "@/hooks/useCredits";
import { CREDIT_PRESETS_CENTS, MIN_CREDITS_CENTS } from "@/lib/creditAmounts";

type Props = { onComplete: () => void };

const SERIF_FONT = { fontFamily: "var(--font-serif), serif" } as const;
const SANS_FONT = { fontFamily: "var(--font-sans), sans-serif" } as const;
const PRIMARY_BUTTON =
  "rounded-full bg-[var(--midnight)] px-6 py-2.5 text-sm font-medium text-cream " +
  "transition-colors hover:bg-[var(--midnight-soft)] disabled:bg-cream-deep disabled:text-[var(--midnight-muted)]";
const TEXT_INPUT =
  "rounded-lg border border-[var(--edge)] bg-cream-deep px-3 py-2.5 text-sm text-[var(--midnight)]";

/** Bedrock onboarding: one workspace top-up request, collected by Metronome. */
export function CreditsStep({ onComplete }: Props) {
  const { scopeKey, balance, refresh, startTopUp, canManageCredits, pendingTopUp, topUpOutcome } = useCredits();
  const [amount, setAmount] = useState(2000);
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState(false);
  const [lockedAmount, setLockedAmount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const advancedRef = useRef(false);
  const scopeGeneration = useRef(0);

  useLayoutEffect(() => {
    scopeGeneration.current += 1;
    setAmount(2000);
    setSubmitting(false);
    setRequested(false);
    setLockedAmount(null);
    setError(null);
    advancedRef.current = false;
    return () => { scopeGeneration.current += 1; };
  }, [scopeKey]);

  useEffect(() => {
    if (advancedRef.current || !balance || isCreditsExhausted(balance)) return;
    advancedRef.current = true;
    onComplete();
  }, [balance, onComplete]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (pendingTopUp) {
      setAmount(pendingTopUp.amount_cents);
      setLockedAmount(pendingTopUp.amount_cents);
      setRequested(true);
    } else {
      setLockedAmount(null);
      setRequested(false);
    }
  }, [pendingTopUp, scopeKey]);

  const requestTopUp = async () => {
    const requestScope = scopeGeneration.current;
    const intentAmount = lockedAmount ?? amount;
    if (lockedAmount === null) setLockedAmount(intentAmount);
    setSubmitting(true);
    setError(null);
    try {
      const result = await startTopUp(intentAmount);
      if (requestScope !== scopeGeneration.current) return;
      if (result.status !== "requested") throw new Error("Top-up was not accepted");
      // Keep the original amount and UUID intent pinned while collection is
      // uncertain. A retry must be the same money request, never a new charge.
      setRequested(true);
      void refresh();
    } catch (cause) {
      if (requestScope === scopeGeneration.current) {
        setError(cause instanceof Error ? cause.message : "Failed to request top-up");
      }
    } finally {
      if (requestScope === scopeGeneration.current) setSubmitting(false);
    }
  };

  return (
    <div className="flex flex-col gap-4 py-8 max-w-md mx-auto" style={SANS_FONT}>
      <h3 className="text-title font-normal text-[var(--midnight)]" style={SERIF_FONT}>Add AI credits</h3>
      <p className="text-sm text-[var(--midnight-muted)]">
        Prepay for managed AI inference. Metronome invoices your workspace&apos;s saved payment method.
      </p>
      {!canManageCredits && <p className="text-sm text-[var(--midnight-muted)]">Only workspace admins can add credits.</p>}
      {canManageCredits && <>
      <div className="grid grid-cols-4 gap-2">
        {CREDIT_PRESETS_CENTS.map((cents) => (
          <button key={cents} type="button" disabled={lockedAmount !== null} onClick={() => setAmount(cents)} style={SANS_FONT}
            className={"rounded-full border px-3 py-2 text-sm transition-colors " +
              (amount === cents ? "border-[var(--midnight)] bg-[var(--cream-deep)] text-[var(--midnight)] font-medium" : "border-[var(--edge)] bg-cream text-[var(--midnight)] hover:bg-[var(--cream-deep)]")}
          >${cents / 100}</button>
        ))}
      </div>
      <input type="number" min={MIN_CREDITS_CENTS / 100} step={5} value={amount / 100} disabled={lockedAmount !== null}
        onChange={(event) => setAmount(Math.round(Number(event.target.value) * 100))} style={SANS_FONT} className={TEXT_INPUT} />
      {error && <p className="text-sm text-[var(--err)]">{error}</p>}
      {topUpOutcome === "failed" && <p className="text-sm text-[var(--err)]">Payment failed. Choose an amount to try again.</p>}
      {requested && <p className="text-sm text-[var(--midnight-muted)]">Payment requested. This step advances once credits land.</p>}
      <button onClick={requestTopUp} disabled={submitting || requested || amount < MIN_CREDITS_CENTS} className={PRIMARY_BUTTON} style={SANS_FONT}>
        {submitting ? "Loading…" : requested ? "Payment requested" : `Add $${amount / 100}`}
      </button>
      </>}
    </div>
  );
}
