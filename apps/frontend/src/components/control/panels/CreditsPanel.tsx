"use client";

import { useState } from "react";
import { Wallet, RefreshCw } from "lucide-react";
import { useCredits } from "@/hooks/useCredits";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";

const CARD = "rounded-lg border border-[#e0dbd0] bg-white p-4 space-y-3";
const EYEBROW = "text-[10px] uppercase tracking-wider text-[#8a8578]/60";

const QUICK_PICKS_CENTS = [1000, 2000, 5000, 10000];

/**
 * Control-panel surface for managing Claude credits. Top-ups go through
 * Stripe Checkout — the user is redirected to checkout.stripe.com and
 * returned to /chat?credits=success on completion. No publishable key
 * needed in the bundle; coupon codes work natively on the Checkout page.
 */
export function CreditsPanel() {
  const { balance, refresh, startTopUp, setAutoReload } = useCredits();
  const [topUpAmount, setTopUpAmount] = useState(2000);
  const [topUpSubmitting, setTopUpSubmitting] = useState(false);
  const [topUpError, setTopUpError] = useState<string | null>(null);
  const [autoEnabled, setAutoEnabled] = useState(false);
  const [thresholdCents, setThresholdCents] = useState(500);
  const [reloadCents, setReloadCents] = useState(2000);

  const handleTopUp = async () => {
    setTopUpSubmitting(true);
    setTopUpError(null);
    try {
      const r = await startTopUp(topUpAmount);
      if (!r?.checkout_url) throw new Error("No checkout_url returned");
      window.location.href = r.checkout_url;
    } catch (err) {
      setTopUpError(err instanceof Error ? err.message : "Couldn't start top-up");
      setTopUpSubmitting(false);
    }
  };

  const handleAutoReloadSave = async () => {
    await setAutoReload({
      enabled: autoEnabled,
      threshold_cents: autoEnabled ? thresholdCents : undefined,
      amount_cents: autoEnabled ? reloadCents : undefined,
    });
  };

  const balanceDisplay = balance ? `$${balance.balance_dollars}` : "$0.00";

  return (
    <div className="p-6 space-y-6">
      <h2 className="text-lg font-semibold">Claude credits</h2>

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
        {topUpError && <p className="text-sm text-red-600">{topUpError}</p>}
        <button
          onClick={handleTopUp}
          disabled={topUpSubmitting || topUpAmount < 500}
          className="rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-4 py-2 text-sm disabled:opacity-50"
        >
          {topUpSubmitting ? "Loading…" : `Add $${topUpAmount / 100}`}
        </button>
        <p className="text-xs text-[#8a8578]">
          Have a promotion code? Apply it on the Stripe Checkout page after clicking above.
        </p>
      </div>

      <div className={CARD}>
        <span className={EYEBROW}>AUTO-RELOAD</span>
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <Checkbox
            checked={autoEnabled}
            onCheckedChange={(v) => setAutoEnabled(v === true)}
          />
          Automatically top up when balance is low
        </label>
        {autoEnabled && (
          <div className="space-y-3 pt-1">
            <div className="space-y-1">
              <label className={EYEBROW}>When balance drops below</label>
              <div className="flex items-center gap-1">
                <span className="text-sm text-[#8a8578]">$</span>
                <Input
                  type="number"
                  min={5}
                  step={5}
                  value={thresholdCents / 100}
                  onChange={(e) =>
                    setThresholdCents(Math.round(Number(e.target.value) * 100))
                  }
                  className="w-24"
                />
              </div>
            </div>
            <div className="space-y-1">
              <label className={EYEBROW}>Charge me</label>
              <div className="flex items-center gap-1">
                <span className="text-sm text-[#8a8578]">$</span>
                {/* min $10: Metronome requires the recharge-to target to exceed
                    the threshold by at least $10 (backend rejects <$10 with a
                    400). Mirror AutoReloadRequest.amount_cents ge=1000. */}
                <Input
                  type="number"
                  min={10}
                  step={5}
                  value={reloadCents / 100}
                  onChange={(e) =>
                    setReloadCents(Math.round(Number(e.target.value) * 100))
                  }
                  className="w-24"
                />
              </div>
            </div>
          </div>
        )}
        <button
          onClick={handleAutoReloadSave}
          className="rounded-md bg-secondary px-4 py-2 text-sm hover:bg-secondary/90"
        >
          Save
        </button>
      </div>
    </div>
  );
}
