"use client";

import { useSyncExternalStore } from "react";
import useSWR from "swr";
import Link from "next/link";
import { useApi } from "@/lib/api";

// Subscribe to a 60-second clock tick. useSyncExternalStore is the
// React 19 idiomatic way to expose mutable external state (current time)
// without tripping the no-impure-during-render or set-state-in-effect
// lint rules.
function subscribeToMinute(onChange: () => void): () => void {
  const t = setInterval(onChange, 60_000);
  return () => clearInterval(t);
}
// The snapshot MUST be stable between calls within a tick: React compares it on every render, and a
// value that changes each call (Date.now()) makes it re-render forever (React #185, the "Something
// went wrong" overlay seen when a document refreshed under an open editor beside the Library).
// The banner counts days, so the current minute is all it needs.
export function getNow(): number {
  return Math.floor(Date.now() / 60_000) * 60_000;
}
function getServerNow(): number {
  // SSR snapshot — return 0 so the banner renders nothing on the server
  // pass and lights up after hydration.
  return 0;
}

// `/billing/account` surfaces `subscription_status` + `trial_end`
// (BillingAccountResponse), set by the trial_will_end +
// customer.subscription.updated webhook branches.
type BillingAccount = {
  subscription_status?: string;
  trial_end?: number; // Unix seconds
  workspace_price_usd?: number;
};

// `subscription_status === "trialing"` is a card-on-file trial: a free-tier
// workspace that checked out early (T05), whose Stripe trial ends on its own
// trial_end, so the first charge is on day 31.
//
// Only surface the in-app banner in the FINAL stretch of the trial: a quiet
// heads-up in the last few days avoids surprise-charge disputes without
// nagging. Calm trial status lives on the Settings → Billing page.
const TRIAL_REMINDER_DAYS = 3;

export function TrialBanner() {
  const api = useApi();
  // No `refreshInterval`: this shares the `/billing/account` key with
  // useBilling, which deliberately sets none, so an interval here was the sole
  // reason that endpoint polled at all. A trial end date moves at most once
  // per billing cycle — the mount read plus useBilling's own revalidations are
  // ample, and the countdown below re-renders off a local clock regardless.
  const { data } = useSWR<BillingAccount | null>(
    "/billing/account",
    (path: string) => api.get(path) as Promise<BillingAccount | null>,
  );

  const now = useSyncExternalStore(subscribeToMinute, getNow, getServerNow);

  if (
    !data ||
    data.subscription_status !== "trialing" ||
    !data.trial_end ||
    now === 0
  ) {
    return null;
  }
  const daysLeft = Math.max(
    0,
    Math.ceil((data.trial_end * 1000 - now) / 86_400_000),
  );
  // Quiet until the final few days — see TRIAL_REMINDER_DAYS.
  if (daysLeft > TRIAL_REMINDER_DAYS) {
    return null;
  }
  // Pin UTC: trial_end is a UTC instant, so a host-local render shows the date
  // a day early west of UTC. Matches Money (money.ts, AgentSpendSection).
  const chargeDate = new Date(data.trial_end * 1000).toLocaleDateString(undefined, {
    timeZone: "UTC",
  });
  const workspaceRate =
    data.workspace_price_usd === undefined
      ? "your workspace rate"
      : `$${data.workspace_price_usd}/mo`;

  return (
    <div className="bg-cream border-b border-info px-4 py-2 text-ui text-info flex items-center justify-between">
      <span>
        Heads up — your free trial ends in{" "}
        <strong>
          {daysLeft === 0 ? "today" : `${daysLeft} day${daysLeft === 1 ? "" : "s"}`}
        </strong>
        {daysLeft === 0 ? "" : ` (${chargeDate})`}, then {workspaceRate}.
      </span>
      <Link href="/settings?panel=billing" className="underline">
        Manage
      </Link>
    </div>
  );
}
