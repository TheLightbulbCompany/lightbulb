"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useCredits } from "@/hooks/useCredits";
import { replaceSearchParams } from "@/lib/shellNav";

type Props = { onComplete: () => void };

const PRESET_AMOUNTS_CENTS = [1000, 2000, 5000, 10000]; // $10, $20, $50, $100

/**
 * Bedrock onboarding step: collect an initial Claude credit top-up.
 *
 * Default path — explicit Stripe Checkout: the customer confirms the
 * payment on a Stripe-hosted page (never an implicit card-on-file charge),
 * optionally ticking the one-click opt-in for future top-ups. The webhook
 * grants the credits the moment payment confirms, so on return the balance
 * poll advances the wizard within seconds.
 *
 * One-click path — only for owners who previously opted in: a payment-gated
 * Metronome commit charges the saved card with no redirect; credits release
 * when the payment collects and the same balance poll advances the wizard.
 */
export function CreditsStep({ onComplete }: Props) {
  const { balance, lifecycleEnabled, autoChargeEnabled, refresh, startTopUp, startTopUpCheckout } =
    useCredits();
  const [amount, setAmount] = useState<number>(2000);
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState(false);
  const [optIn, setOptIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const advancedRef = useRef(false);
  const searchParams = useSearchParams();
  // Returning from a cancelled Checkout: surface it instead of a blank form.
  const checkoutCancelled = searchParams.get("credits") === "cancel";

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
    replaceSearchParams(params);
  }, [searchParams]);

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

  // One-click path (opted-in owners only): payment-gated commit against the
  // saved card, no redirect.
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

  // Default path: hand off to the Stripe-hosted Checkout. No latch needed —
  // the browser leaves this page; on return the balance poll advances.
  const goToCheckout = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await startTopUpCheckout(amount, optIn);
      if (!r?.checkout_url) throw new Error("Checkout could not be started");
      window.location.assign(r.checkout_url);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to start checkout");
      setSubmitting(false);
    }
  };

  return (
    <div className="flex flex-col gap-4 py-8 max-w-md mx-auto">
      <h3 className="text-xl font-semibold">Add Claude credits</h3>
      <p className="text-sm text-muted-foreground">
        {autoChargeEnabled
          ? "Prepay for Claude inference — charged automatically to your card on file. Add any amount, top up later anytime."
          : "Prepay for Claude inference. You'll confirm the payment on a secure Stripe page — nothing is charged until you do."}
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

      {!autoChargeEnabled && (
        <label className="flex items-start gap-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            checked={optIn}
            onChange={(e) => setOptIn(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            Enable one-click top-ups — save this card so future credit
            purchases charge it automatically, no checkout page. You can turn
            this off anytime.
          </span>
        </label>
      )}

      {checkoutCancelled && !requested && !error && (
        <p className="text-sm text-muted-foreground">
          Checkout was cancelled — nothing was charged. Pick an amount to try
          again.
        </p>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
      {requested && (
        <p className="text-sm text-muted-foreground">
          Payment requested — your card on file is being charged in the
          background. This step advances automatically once the credits land
          (usually under a minute).
        </p>
      )}

      {autoChargeEnabled ? (
        <button
          onClick={requestTopUp}
          disabled={submitting || requested || amount < 500}
          className="rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
        >
          {submitting ? "Loading…" : requested ? "Payment requested" : `Add $${amount / 100}`}
        </button>
      ) : (
        <button
          onClick={goToCheckout}
          disabled={submitting || amount < 500}
          className="rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
        >
          {submitting ? "Redirecting…" : `Continue to payment — $${amount / 100}`}
        </button>
      )}
    </div>
  );
}
