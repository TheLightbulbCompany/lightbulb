"use client";

import { useState } from "react";
import { capture } from "@/lib/analytics";
import type { BillingAccount } from "@/hooks/useBilling";
import { markupPercent } from "@/lib/pricing";

/**
 * The one plan (T06) on Settings → Billing: Team, $workspace_price_usd a month
 * for up to `included_members` people, then $seat_price_usd for each member
 * past them. Base and Ultra are retired; a workspace still billed on one sees
 * "Switch to Team". Prices come from /billing/account, never baked in. The
 * button goes through `startCheckout` (POST /billing/checkout): a new
 * subscription opens Checkout, a live one is swapped in place after a confirm.
 */
export function PlanChooser({
  account,
  subscribed,
  startCheckout,
}: {
  account: BillingAccount | null | undefined;
  /** False for the free trial: nothing is current, the button starts the plan. */
  subscribed: boolean;
  /** `confirm` is required by the server to change a LIVE subscription's plan. */
  startCheckout: (plan: "workspace", confirm: boolean) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const price = account?.workspace_price_usd;
  const seat = account?.seat_price_usd;
  const included = account?.included_members;
  const current = subscribed && (account?.plan ?? "standard") === "standard";
  const changing = subscribed && !current;

  const choose = async () => {
    if (changing) {
      const ok = window.confirm(
        `Switch to Team${price === undefined ? "" : ` ($${price}/mo)`}?\n\n` +
          "Your plan changes now. The prorated difference for the rest of this billing period " +
          "is added to your next invoice.",
      );
      if (!ok) return;
    }
    capture("billing_plan_chosen", { plan: "workspace" });
    setError(null);
    setPending(true);
    try {
      await startCheckout("workspace", changing);
    } catch {
      setError("Couldn't start the plan. Please try again.");
    } finally {
      setPending(false);
    }
  };

  return (
    <div data-testid="plan-chooser">
      <div data-testid="plan-workspace" className="flex flex-col rounded-xl border border-[var(--edge)] bg-cream p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium text-[var(--midnight)]">Keep your team on Lightbulb</span>
          {current && <span className="label-caps">Current plan</span>}
        </div>
        {price !== undefined && included !== undefined && seat !== undefined ? (
          <p className="mt-1 text-sm text-[var(--midnight)]">
            ${price} a month for up to {included} people. ${seat} a month for each member from the{" "}
            {included + 1}th.
          </p>
        ) : (
          <p className="mt-1 text-sm text-[var(--midnight)]">—</p>
        )}
        <ul className="mt-3 space-y-1.5 text-xs text-[var(--midnight-muted)]">
          <li>
            Includes: Lightbulb and its specialists, their memory and email, the shared browser, the always-on
            runtime.
          </li>
          <li>
            AI usage: bring your ChatGPT account or API key at no charge, or use Lightbulb credits
            {account?.managed_markup
              ? ` at the model's price plus ${markupPercent(account.managed_markup)}%.`
              : "."}
          </li>
        </ul>
        {!current && (
          <button
            type="button"
            className="settings-btn-save mt-4 self-start"
            onClick={() => void choose()}
            disabled={pending}
          >
            {pending
              ? "Starting…"
              : changing
                ? "Switch to Team"
                : `Continue${price === undefined ? "" : ` for $${price} a month`}`}
          </button>
        )}
        <p className="mt-3 text-xs text-[var(--midnight-muted)]">
          Billed monthly. Cancel any time from this page; your workspace stays readable.
        </p>
      </div>
      {error && (
        <div className="settings-alert settings-alert-error" style={{ marginTop: 8 }}>
          {error}
        </div>
      )}
    </div>
  );
}
