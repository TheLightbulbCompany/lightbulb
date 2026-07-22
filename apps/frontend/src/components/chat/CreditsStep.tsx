"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useCredits } from "@/hooks/useCredits";
import { replaceSearchParams } from "@/lib/shellNav";

type Props = { onComplete: () => void };

const PRESET_AMOUNTS_CENTS = [1000, 2000, 5000, 10000]; // $10, $20, $50, $100

// Onboarding design system — mirrors the stepper branch this step renders
// inside (cream page, forest CTAs, Lora headings, DM Sans body). Fonts ride
// the next/font CSS vars via inline styles rather than Tailwind font classes,
// the same way the stepper's own cards do.
const SERIF_FONT = { fontFamily: "var(--font-lora-serif), serif" } as const;
const SANS_FONT = { fontFamily: "var(--font-dm-sans), sans-serif" } as const;

const PRIMARY_BUTTON =
  "rounded-full bg-[#06402B] px-6 py-2.5 text-sm font-medium text-white " +
  "transition-colors hover:bg-[#052e1f] disabled:opacity-50 disabled:hover:bg-[#06402B]";

const TEXT_INPUT =
  "rounded-lg border border-[#e0dbd0] bg-white px-3 py-2.5 text-sm text-[#1a1a1a] " +
  "focus:outline-none focus:border-[rgba(6,64,43,0.25)] focus:ring-2 focus:ring-[rgba(6,64,43,0.25)]";

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
      <div
        className="flex flex-col gap-4 py-8 max-w-md mx-auto text-center"
        style={SANS_FONT}
      >
        <h3 className="text-[22px] font-normal text-[#1a1a1a]" style={SERIF_FONT}>
          Claude credits aren&apos;t available yet
        </h3>
        <p className="text-sm text-[#8a8578]">
          Claude credits are not yet available in this environment — choose a
          different provider for now.
        </p>
        <button
          type="button"
          onClick={chooseDifferentProvider}
          className={PRIMARY_BUTTON}
          style={SANS_FONT}
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
    <div className="flex flex-col gap-4 py-8 max-w-md mx-auto" style={SANS_FONT}>
      <h3 className="text-[22px] font-normal text-[#1a1a1a]" style={SERIF_FONT}>
        Add Claude credits
      </h3>
      <p className="text-sm text-[#8a8578]">
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
            style={SANS_FONT}
            className={
              "rounded-full border px-3 py-2 text-sm transition-colors " +
              (amount === c
                ? "border-[#06402B] bg-[#e6efe9] text-[#06402B] font-medium"
                : "border-[#e0dbd0] bg-white text-[#1a1a1a] hover:bg-[#f3efe6]")
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
        style={SANS_FONT}
        className={TEXT_INPUT}
      />

      {!autoChargeEnabled && (
        <label className="flex items-start gap-2 text-[13px] text-[#8a8578]">
          <input
            type="checkbox"
            checked={optIn}
            onChange={(e) => setOptIn(e.target.checked)}
            className="mt-0.5 accent-[#06402B]"
          />
          <span>
            Enable one-click top-ups — save this card so future credit
            purchases charge it automatically, no checkout page. You can turn
            this off anytime.
          </span>
        </label>
      )}

      {checkoutCancelled && !requested && !error && (
        <p className="text-sm text-[#8a8578]">
          Checkout was cancelled — nothing was charged. Pick an amount to try
          again.
        </p>
      )}
      {error && <p className="text-sm text-[#b32424]">{error}</p>}
      {requested && (
        <p className="text-sm text-[#8a8578]">
          Payment requested — your card on file is being charged in the
          background. This step advances automatically once the credits land
          (usually under a minute).
        </p>
      )}

      {autoChargeEnabled ? (
        <button
          onClick={requestTopUp}
          disabled={submitting || requested || amount < 500}
          className={PRIMARY_BUTTON}
          style={SANS_FONT}
        >
          {submitting ? "Loading…" : requested ? "Payment requested" : `Add $${amount / 100}`}
        </button>
      ) : (
        <button
          onClick={goToCheckout}
          disabled={submitting || amount < 500}
          className={PRIMARY_BUTTON}
          style={SANS_FONT}
        >
          {submitting ? "Redirecting…" : `Continue to payment — $${amount / 100}`}
        </button>
      )}
    </div>
  );
}
