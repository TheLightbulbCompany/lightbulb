"use client";
import { useEffect, useRef, useState } from "react";
import { useCredits } from "@/hooks/useCredits";

type Props = { onComplete: () => void };

const PRESET_AMOUNTS_CENTS = [1000, 2000, 5000, 10000]; // $10, $20, $50, $100

/**
 * Bedrock onboarding step: collect an initial Claude credit top-up. The
 * request creates a payment-gated Metronome commit charged automatically to
 * the card saved during the subscription checkout — no redirect. Credits
 * release when the payment collects, so after requesting we keep polling
 * the balance (via useCredits) and auto-advance the wizard once it flips
 * positive.
 */
export function CreditsStep({ onComplete }: Props) {
  const { balance, refresh, startTopUp } = useCredits();
  const [amount, setAmount] = useState<number>(2000);
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const advancedRef = useRef(false);

  // On mount and on every balance bump, check whether we already have
  // credits — if so, a previous top-up has landed (or the owner arrived
  // here with a pre-existing balance) and the wizard should skip past the
  // top-up step.
  useEffect(() => {
    if (advancedRef.current) return;
    if (balance && balance.balance_microcents !== null && balance.balance_microcents > 0) {
      advancedRef.current = true;
      onComplete();
    }
  }, [balance, onComplete]);

  // Force a balance refresh on mount so a just-released top-up is detected
  // without waiting the SWR refresh interval.
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const requestTopUp = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await startTopUp(amount);
      if (r?.status !== "requested") throw new Error("Top-up was not accepted");
      setRequested(true);
      void refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to request top-up");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex flex-col gap-4 py-8 max-w-md mx-auto">
      <h3 className="text-xl font-semibold">Add Claude credits</h3>
      <p className="text-sm text-muted-foreground">
        Prepay for Claude inference — charged automatically to your card on
        file. Add any amount, top up later anytime.
      </p>

      <div className="grid grid-cols-4 gap-2">
        {PRESET_AMOUNTS_CENTS.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setAmount(c)}
            className={
              "rounded-md border px-3 py-2 text-sm " +
              (amount === c
                ? "border-primary bg-primary/10"
                : "border-border bg-card")
            }
          >
            ${c / 100}
          </button>
        ))}
      </div>

      <input
        type="number"
        min={5}
        step={5}
        value={amount / 100}
        onChange={(e) => setAmount(Math.round(Number(e.target.value) * 100))}
        className="rounded-md border border-input bg-background px-3 py-2"
      />

      {error && <p className="text-sm text-destructive">{error}</p>}
      {requested && (
        <p className="text-sm text-muted-foreground">
          Top-up requested — waiting for the payment to collect. This step
          advances automatically once your credits land.
        </p>
      )}

      <button
        onClick={requestTopUp}
        disabled={submitting || amount < 500}
        className="rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
      >
        {submitting ? "Loading…" : `Add $${amount / 100}`}
      </button>
    </div>
  );
}
