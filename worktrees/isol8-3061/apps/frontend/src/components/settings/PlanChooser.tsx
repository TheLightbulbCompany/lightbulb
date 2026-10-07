"use client";

import { useState } from "react";
import { capture } from "@/lib/analytics";
import type { BillingAccount } from "@/hooks/useBilling";

type CheckoutPlan = "base" | "workspace" | "ultra";

// Ordered cheapest first; `stored` is the billing row's `plan` value. Features are
// what the backend actually differentiates today: Base IS the capped managed model
// (trial_cap.is_managed_capped); a paid plan is always-on (has_always_on_entitlement);
// ChatGPT/own key are open to every paid plan (change_provider has no plan gate);
// prepaid credits need Workspace/Ultra (metronome_funding). Ultra has no extra gate yet.
const PLANS: { key: CheckoutPlan; stored: BillingAccount["plan"]; name: string; features: string[] }[] = [
  {
    key: "base",
    stored: "base",
    name: "Base",
    features: [
      "Lightbulb-managed DeepSeek within a daily allowance",
      "Or sign in with ChatGPT / use your own API key (billed by that provider)",
      "Always-on agents: listening, routines, webhooks",
    ],
  },
  {
    key: "workspace",
    stored: "standard",
    name: "Workspace",
    features: [
      "Everything in Base",
      "More models with prepaid credits (coming soon)",
    ],
  },
  { key: "ultra", stored: "ultra_pro", name: "Ultra", features: ["Everything in Workspace"] },
];

/**
 * The one plan ladder on Settings → Billing: Base → Workspace → Ultra side by side,
 * prices from /billing/account (never baked in), the current plan marked. Every
 * button goes through `startCheckout` (POST /billing/checkout), which opens Checkout
 * for a new subscription or swaps the live one in place.
 */
export function PlanChooser({
  account,
  subscribed,
  startCheckout,
}: {
  account: BillingAccount | null | undefined;
  /** False for the free trial: no plan is current, every card starts one. */
  subscribed: boolean;
  startCheckout: (plan: CheckoutPlan) => Promise<void>;
}) {
  const [pending, setPending] = useState<CheckoutPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const prices: Record<CheckoutPlan, number | undefined> = {
    base: account?.base_price_usd,
    workspace: account?.workspace_price_usd,
    ultra: account?.ultra_price_usd,
  };
  const currentIndex = subscribed ? PLANS.findIndex((p) => p.stored === (account?.plan ?? "standard")) : -1;

  const choose = async (plan: CheckoutPlan) => {
    capture("billing_plan_chosen", { plan });
    setError(null);
    setPending(plan);
    try {
      // Fresh Checkout redirects away; an in-place swap resolves and the refreshed
      // account re-marks the current plan.
      await startCheckout(plan);
    } catch {
      setError("Couldn't start the plan change. Please try again.");
    } finally {
      setPending(null);
    }
  };

  return (
    <div data-testid="plan-chooser">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {PLANS.map((plan, i) => {
          const price = prices[plan.key];
          const current = i === currentIndex;
          const verb = currentIndex === -1 ? "Choose" : i > currentIndex ? "Upgrade to" : "Switch to";
          return (
            <div
              key={plan.key}
              data-testid={`plan-${plan.key}`}
              className={`flex flex-col rounded-xl border bg-cream p-4 ${current ? "border-[var(--midnight)]" : "border-[var(--edge)]"}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-[var(--midnight)]">{plan.name}</span>
                {current && <span className="label-caps">Current plan</span>}
              </div>
              <p className="mt-1 text-sm text-[var(--midnight)]">{price === undefined ? "—" : `$${price}/mo`}</p>
              <ul className="mt-3 flex-1 space-y-1.5 text-xs text-[var(--midnight-muted)]">
                {plan.features.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
              {!current && (
                <button
                  type="button"
                  className="settings-btn-save mt-4"
                  onClick={() => void choose(plan.key)}
                  disabled={pending !== null}
                >
                  {pending === plan.key ? "Starting…" : `${verb} ${plan.name}`}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {error && (
        <div className="settings-alert settings-alert-error" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
    </div>
  );
}
