"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useBilling } from "@/hooks/useBilling";
import { replaceSearchParams } from "@/lib/shellNav";
import { markCheckoutReturn } from "@/lib/checkoutReturn";

export function useCheckoutReturn(): { showSubscriptionSuccess: boolean } {
  const searchParams = useSearchParams();
  const { refresh: refreshBilling, isSubscribed } = useBilling();

  // Stripe Checkout returns either ?subscription=success (legacy) or
  // ?checkout=success (workspace checkout). Both should trigger the
  // billing-refresh + URL-cleanup effect.
  //
  // Two separate state slices keyed off the same URL param so the banner
  // and the polling lifecycle are independent — banner auto-dismisses at
  // 5s, polling continues for up to 30s. Without this split the
  // banner-dismiss tear-down clobbers the poll interval.
  const cameFromCheckout =
    searchParams.get("subscription") === "success" ||
    searchParams.get("checkout") === "success";
  const [showSubscriptionSuccess, setShowSubscriptionSuccess] = useState(() => cameFromCheckout);
  const [pollingForWebhook, setPollingForWebhook] = useState(() => cameFromCheckout);

  // Post-checkout confirmation: refresh billing + clean URL.
  // Strip ONLY ?checkout / ?subscription, preserving ?provider= so the
  // ProvisioningStepper can still render the ChatGPT OAuth step — without
  // this, the wizard never gets to the step
  // that collects the user's credentials.
  useEffect(() => {
    if (!showSubscriptionSuccess) return;

    // Remembered before the strip below, for screens that mount later (ChatGPTPlanGate).
    markCheckoutReturn();
    refreshBilling();
    const params = new URLSearchParams(searchParams.toString());
    params.delete("checkout");
    params.delete("subscription");
    replaceSearchParams(params);

    const dismissTimer = setTimeout(() => setShowSubscriptionSuccess(false), 5000);
    return () => clearTimeout(dismissTimer);
  }, [showSubscriptionSuccess, refreshBilling, searchParams]);

  // Race against Stripe webhook delivery: the user returns from Checkout
  // before customer.subscription.created lands at our webhook endpoint
  // (typically <2s, but variable). Without polling, the wizard would
  // bounce back to the ProviderPicker because is_subscribed stays False
  // until the webhook persists subscription_status. Poll every 2s for up
  // to 30s — once is_subscribed flips True we stop early, otherwise we
  // give up so we don't poll forever.
  //
  // Keyed on `pollingForWebhook` (independent of the banner state) so the
  // banner's 5s auto-dismiss doesn't cancel the longer poll loop.
  useEffect(() => {
    if (!pollingForWebhook || isSubscribed) return;
    const pollInterval = setInterval(() => refreshBilling(), 2000);
    const giveUpTimer = setTimeout(() => setPollingForWebhook(false), 30_000);
    return () => {
      clearInterval(pollInterval);
      clearTimeout(giveUpTimer);
    };
  }, [pollingForWebhook, isSubscribed, refreshBilling]);

  return { showSubscriptionSuccess };
}
