"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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
  const { balance, lifecycleEnabled, refresh, startTopUp } = useCredits();
  const [amount, setAmount] = useState<number>(2000);
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const advancedRef = useRef(false);
  const router = useRouter();
  const searchParams = useSearchParams();

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

  // The stepper's only "go back" mechanism is the `?provider=` URL param:
  // stripping it exits the provider step and re-enters the main flow (the
  // provider picker for a not-yet-subscribed owner, normal provisioning
  // otherwise). Mirrors the stepper's own strip-effect so other params
  // (e.g. checkout=success) survive.
  const chooseDifferentProvider = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("provider");
    const remaining = params.toString();
    router.replace(`/chat${remaining ? `?${remaining}` : ""}`, { scroll: false });
  }, [router, searchParams]);

  // Credits lifecycle not wired for this environment: every top-up request
  // would 400, so the form is a dead end — offer the way back instead.
  if (lifecycleEnabled === false) {
    return (
      <div className="flex flex-col gap-4 py-8 max-w-md mx-auto text-center">
        <h3 className="text-xl font-semibold">Claude credits aren&apos;t available yet</h3>
        <p className="text-sm text-muted-foreground">
          Claude credits are not yet available in this environment — choose a
          different provider for now.
        </p>
        <button
          type="button"
          onClick={chooseDifferentProvider}
          className="rounded-md bg-primary px-4 py-2 text-primary-foreground"
        >
          Choose a different provider
        </button>
      </div>
    );
  }

  const requestTopUp = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await startTopUp(amount);
      if (r?.status !== "requested") throw new Error("Top-up was not accepted");
      // Latch `requested` and leave the button disabled: the payment
      // collects asynchronously, so re-enabling here would invite a second
      // identical submit (= a second card charge) while the first is still
      // landing. The balance poll advances the wizard once credits arrive.
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
          Top-up requested — the payment collects in the background, so
          credits can take a few minutes to land. This step advances
          automatically once they do.
        </p>
      )}

      <button
        onClick={requestTopUp}
        disabled={submitting || requested || amount < 500}
        className="rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
      >
        {submitting ? "Loading…" : requested ? "Top-up requested" : `Add $${amount / 100}`}
      </button>
    </div>
  );
}
