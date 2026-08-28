"use client";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useCredits, isCreditsExhausted } from "@/hooks/useCredits";
import { CREDIT_PRESETS_CENTS, MIN_CREDITS_CENTS } from "@/lib/creditAmounts";

type Props = {
  onComplete: () => void;
  /**
   * The Checkout that just completed already bought these credits as a
   * one-time line item, so the grant is travelling through the webhook right
   * now. Render a waiting state instead of the purchase form: the skip effect
   * below only fires once the balance READS spendable, and at the moment the
   * browser returns from Stripe it does not — which put a live "pay again"
   * form in front of someone who had just paid to avoid exactly that.
   */
  awaitingBundledGrant?: boolean;
};

// Shared ladder — see lib/creditAmounts. This used to be a separate array
// offering $10 where the bundled picker offered $5.
const PRESET_AMOUNTS_CENTS = CREDIT_PRESETS_CENTS;

// Grace window for the one-click `requested` latch before it un-sticks itself
// with a retryable error. Comfortably longer than the "usually under a minute"
// happy path so a normally-collecting charge advances the wizard first.
const REQUESTED_TIMEOUT_MS = 90_000;

// Same backstop, same reason, for the bundled-grant wait. The money is ALREADY
// collected by the subscription Checkout at this point, so a grant that never
// lands (a persistent Metronome failure rather than a transient ordering race)
// would otherwise park the owner on a spinner forever with no indication that
// anything is wrong and no route to support. Stripe's redelivery + the
// reconciling meter make a permanent stall unlikely, which is why this
// surfaces a message rather than retrying harder.
const BUNDLED_GRANT_TIMEOUT_MS = 90_000;

// Onboarding design system — mirrors the stepper branch this step renders
// inside (cream page, forest CTAs, Lora headings, DM Sans body). Fonts ride
// the next/font CSS vars via inline styles rather than Tailwind font classes,
// the same way the stepper's own cards do.
const SERIF_FONT = { fontFamily: "var(--font-serif), serif" } as const;
const SANS_FONT = { fontFamily: "var(--font-sans), sans-serif" } as const;

const PRIMARY_BUTTON =
  "rounded-full bg-[var(--midnight)] px-6 py-2.5 text-sm font-medium text-cream " +
  "transition-colors hover:bg-[var(--midnight-soft)] disabled:opacity-50 disabled:hover:bg-[var(--midnight)]";

const TEXT_INPUT =
  "rounded-lg border border-[var(--edge)] bg-cream px-3 py-2.5 text-sm text-[var(--midnight)] " +
  "focus:outline-none focus:border-[var(--midnight)] focus:ring-2 focus:ring-ring";

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
export function CreditsStep({ onComplete, awaitingBundledGrant = false }: Props) {
  const {
    balance,
    autoChargeEnabled,
    refresh,
    startTopUp,
    rotateRequestId,
    startTopUpCheckout,
  } = useCredits();
  const [amount, setAmount] = useState<number>(2000);
  const [submitting, setSubmitting] = useState(false);
  const [requested, setRequested] = useState(false);
  const [optIn, setOptIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bundledGrantSlow, setBundledGrantSlow] = useState(false);
  const advancedRef = useRef(false);
  const searchParams = useSearchParams();
  // Returning from a cancelled Checkout: surface it instead of a blank form.
  const checkoutCancelled = searchParams.get("credits") === "cancel";

  // On mount and on every balance bump, check whether the balance is already
  // spendable — if so, a previous top-up has landed (or the owner arrived
  // here with a pre-existing balance) and the wizard should skip past the
  // top-up step. Uses the same isCreditsExhausted predicate as
  // OutOfCreditsBanner (see useCredits.ts) rather than a raw `> 0` check: the
  // bedrock-gate refuses admission below its own reservation threshold, so a
  // small positive balance (e.g. $0.20) is NOT spendable — completing here
  // for it would land the owner back in chat facing the same "out of
  // credits" banner with no way to reach this top-up form again.
  useEffect(() => {
    if (advancedRef.current) return;
    if (balance && !isCreditsExhausted(balance)) {
      advancedRef.current = true;
      onComplete();
    }
  }, [balance, onComplete]);

  // Force a balance refresh on mount so a just-released top-up is detected
  // without waiting the SWR refresh interval.
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Time-box the one-click `requested` latch. The latch keeps the button
  // disabled while the async charge collects and credits are expected to land
  // shortly (the balance poll then advances the wizard). But if the charge
  // silently FAILS after acceptance (e.g. the card is declined once it
  // actually collects), no balance ever arrives and the button would stay
  // "Payment requested" forever — a dead end. So after a grace window, if the
  // wizard hasn't advanced (advancedRef still false), un-stick the button and
  // surface a retryable error. Pure client-side backstop: a precise decline
  // reason would need a backend charge-status field, but this alone kills the
  // stuck-forever state.
  //
  // Rotate the idempotency token FIRST: this timeout means the prior attempt is
  // presumed dead, so the next click must be a NEW top-up intent. Reusing the
  // token would replay the accepted-but-uncollected commit (or 409 on an
  // idempotency/body conflict if the amount changed), re-sticking the user in
  // the same failed cycle.
  useEffect(() => {
    if (!requested) return;
    const t = setTimeout(() => {
      if (advancedRef.current) return;
      rotateRequestId();
      setRequested(false);
      setError(
        "That's taking longer than expected — the charge may not have gone through. You can try again.",
      );
    }, REQUESTED_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [requested, rotateRequestId]);

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

  // Bundled-grant backstop — parity with the one-click latch above.
  useEffect(() => {
    if (!awaitingBundledGrant) return;
    const t = setTimeout(() => {
      if (advancedRef.current) return;
      setBundledGrantSlow(true);
    }, BUNDLED_GRANT_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [awaitingBundledGrant]);

  // Already paid for in the subscription Checkout — wait for the grant, never
  // offer a second purchase. The balance poll above still runs, so this
  // advances on its own; the refresh button is only for impatience.
  if (awaitingBundledGrant) {
    return (
      <div className="flex flex-col gap-4 py-8 max-w-md mx-auto text-center" style={SANS_FONT}>
        <h3 className="text-[22px] font-normal text-[var(--midnight)]" style={SERIF_FONT}>
          Adding your Claude credits
        </h3>
        <p className="text-sm text-[var(--midnight-muted)]">
          Your payment went through. The credits are being added to your
          balance — this usually takes a few seconds and continues on its own.
        </p>
        {bundledGrantSlow && (
          <p className="text-sm text-[var(--err)]">
            This is taking longer than expected. Your payment was received and
            the credits will still land — if this doesn&apos;t clear shortly,
            contact support and we&apos;ll finish it for you.
          </p>
        )}
        <button onClick={() => void refresh()} className={PRIMARY_BUTTON} style={SANS_FONT}>
          Check again
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 py-8 max-w-md mx-auto" style={SANS_FONT}>
      <h3 className="text-[22px] font-normal text-[var(--midnight)]" style={SERIF_FONT}>
        Add Claude credits
      </h3>
      <p className="text-sm text-[var(--midnight-muted)]">
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
                ? "border-[var(--midnight)] bg-[var(--cream-deep)] text-[var(--midnight)] font-medium"
                : "border-[var(--edge)] bg-cream text-[var(--midnight)] hover:bg-[var(--cream-deep)]")
            }
          >
            ${c / 100}
          </button>
        ))}
      </div>

      <input
        type="number"
        min={MIN_CREDITS_CENTS / 100}
        step={5}
        value={amount / 100}
        onChange={(e) => setAmount(Math.round(Number(e.target.value) * 100))}
        style={SANS_FONT}
        className={TEXT_INPUT}
      />

      {!autoChargeEnabled && (
        <label className="flex items-start gap-2 text-[13px] text-[var(--midnight-muted)]">
          <input
            type="checkbox"
            checked={optIn}
            onChange={(e) => setOptIn(e.target.checked)}
            className="mt-0.5 accent-[var(--midnight)]"
          />
          <span>
            Enable one-click top-ups — save this card so future credit
            purchases charge it automatically, no checkout page. You can turn
            this off anytime.
          </span>
        </label>
      )}

      {checkoutCancelled && !requested && !error && (
        <p className="text-sm text-[var(--midnight-muted)]">
          Checkout was cancelled — nothing was charged. Pick an amount to try
          again.
        </p>
      )}
      {error && <p className="text-sm text-[var(--err)]">{error}</p>}
      {requested && (
        <p className="text-sm text-[var(--midnight-muted)]">
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
