"use client";

import { useState } from "react";
import { Wallet, RefreshCw } from "lucide-react";
import { useCredits } from "@/hooks/useCredits";

const CARD = "rounded-lg border border-[#e0dbd0] bg-white p-4 space-y-3";
const EYEBROW = "text-[10px] uppercase tracking-wider text-[#8a8578]/60";

const QUICK_PICKS_CENTS = [1000, 2000, 5000, 10000];

/**
 * Embeddable Claude-credits manager (balance + top-up + auto-reload).
 * Mounted inside the Model & Credits panel for bedrock_claude owners —
 * this is the only credits surface; there is no standalone Credits panel.
 * Purchase-path semantics are unchanged: explicit Stripe Checkout by
 * default, one-click Metronome commit for owners who opted in.
 */
export function CreditsSection() {
  const {
    balance,
    lifecycleEnabled,
    autoChargeEnabled,
    refresh,
    startTopUp,
    startTopUpCheckout,
    disableAutoCharge,
  } = useCredits();
  const [topUpAmount, setTopUpAmount] = useState(2000);
  const [topUpSubmitting, setTopUpSubmitting] = useState(false);
  const [topUpError, setTopUpError] = useState<string | null>(null);
  const [topUpRequested, setTopUpRequested] = useState<number | null>(null);
  const [optIn, setOptIn] = useState(false);
  const [disabling, setDisabling] = useState(false);

  const handleTopUp = async () => {
    setTopUpSubmitting(true);
    setTopUpError(null);
    setTopUpRequested(null);
    try {
      const r = await startTopUp(topUpAmount);
      if (r?.status !== "requested") throw new Error("Top-up was not accepted");
      // Latch `topUpRequested` and leave the button disabled: the payment
      // collects asynchronously, so re-enabling would invite a second
      // identical submit (= a second card charge) while the first is still
      // landing. The error path clears it so a failed attempt stays
      // retryable.
      setTopUpRequested(r.amount_cents);
      void refresh();
    } catch (err) {
      setTopUpError(err instanceof Error ? err.message : "Couldn't start top-up");
    } finally {
      setTopUpSubmitting(false);
    }
  };

  const handleCheckout = async () => {
    setTopUpSubmitting(true);
    setTopUpError(null);
    try {
      const r = await startTopUpCheckout(topUpAmount, optIn);
      if (!r?.checkout_url) throw new Error("Checkout could not be started");
      window.location.assign(r.checkout_url);
    } catch (err) {
      setTopUpError(err instanceof Error ? err.message : "Couldn't start checkout");
      setTopUpSubmitting(false);
    }
  };

  const handleDisableAutoCharge = async () => {
    setDisabling(true);
    try {
      await disableAutoCharge();
    } finally {
      setDisabling(false);
    }
  };

  const balanceDisplay =
    balance && balance.balance_dollars !== null ? `$${balance.balance_dollars}` : "—";

  return (
    <div className="space-y-6">
      <div className={CARD}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Wallet className="h-3.5 w-3.5 text-[#8a8578]" />
            <span className={EYEBROW}>BALANCE</span>
          </div>
          <button
            type="button"
            onClick={() => refresh()}
            aria-label="Refresh"
            className="text-[#8a8578] hover:text-[#1a1a1a]"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="text-3xl font-semibold font-mono text-[#1a1a1a]">{balanceDisplay}</div>
        {balance && balance.balance_dollars === null && (
          <p className="text-xs text-[#8a8578]">
            Balance unavailable right now — try refreshing in a moment.
          </p>
        )}
      </div>

      <div className={CARD}>
        <span className={EYEBROW}>ADD CREDITS</span>
        <div className="flex flex-wrap gap-2">
          {QUICK_PICKS_CENTS.map((c) => {
            const active = topUpAmount === c;
            return (
              <button
                key={c}
                onClick={() => setTopUpAmount(c)}
                className={
                  "rounded-md border px-3 py-1.5 text-sm transition-colors " +
                  (active
                    ? "border-[#06402B] bg-[#06402B]/5 text-[#06402B]"
                    : "border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6]")
                }
              >
                ${c / 100}
              </button>
            );
          })}
        </div>
        {!autoChargeEnabled && (
          <label className="flex items-start gap-2 text-xs text-[#8a8578]">
            <input
              type="checkbox"
              checked={optIn}
              onChange={(e) => setOptIn(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              Enable one-click top-ups — save this card so future purchases
              charge it automatically. You can turn this off anytime.
            </span>
          </label>
        )}
        {topUpError && <p className="text-sm text-red-600">{topUpError}</p>}
        {topUpRequested !== null && (
          <p className="text-sm text-[#2d8a4e]">
            Top-up of ${topUpRequested / 100} requested — your card on file will be
            charged and credits can take a few minutes to land.
          </p>
        )}
        {autoChargeEnabled ? (
          <button
            onClick={handleTopUp}
            disabled={
              topUpSubmitting ||
              topUpRequested !== null ||
              lifecycleEnabled === false ||
              topUpAmount < 500
            }
            className="rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-4 py-2 text-sm disabled:opacity-50"
          >
            {topUpSubmitting
              ? "Loading…"
              : topUpRequested !== null
                ? "Top-up requested"
                : `Add $${topUpAmount / 100}`}
          </button>
        ) : (
          <button
            onClick={handleCheckout}
            disabled={topUpSubmitting || lifecycleEnabled === false || topUpAmount < 500}
            className="rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-4 py-2 text-sm disabled:opacity-50"
          >
            {topUpSubmitting ? "Redirecting…" : `Continue to payment — $${topUpAmount / 100}`}
          </button>
        )}
        <p className="text-xs text-[#8a8578]">
          {lifecycleEnabled === false
            ? "Claude credits are not yet available in this environment — choose a different provider for now."
            : autoChargeEnabled
              ? "Charged automatically to your card on file — no checkout page."
              : "You'll confirm the payment on a secure Stripe page — nothing is charged until you do."}
        </p>
        {autoChargeEnabled && (
          <button
            type="button"
            onClick={handleDisableAutoCharge}
            disabled={disabling}
            className="text-xs text-[#8a8578] underline hover:text-[#1a1a1a] disabled:opacity-50 text-left"
          >
            {disabling ? "Disabling…" : "Disable one-click top-ups"}
          </button>
        )}
      </div>
    </div>
  );
}
