"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useBilling } from "@/hooks/useBilling";
import { replaceSearchParams } from "@/lib/shellNav";

export function useCheckoutReturn(): { showSubscriptionSuccess: boolean } {
  const searchParams = useSearchParams();
  const { refresh: refreshBilling, isSubscribed } = useBilling();

  // Stripe Checkout returns either ?subscription=success (legacy) or
  // ?checkout=success (new trial-checkout flow). Both should trigger the
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
  // ProvisioningStepper can still render the provider-specific step
  // (ChatGPTOAuthStep / ByoKeyStep / CreditsStep) — without this, the
  // OAuth + BYO paths break because the wizard never gets to the step
  // that collects the user's credentials.
  useEffect(() => {
    if (!showSubscriptionSuccess) return;

    refreshBilling();
    const params = new URLSearchParams(searchParams.toString());
    params.delete("checkout");
    params.delete("subscription");
    replaceSearchParams(params);

    const dismissTimer = setTimeout(() => setShowSubscriptionSuccess(false), 5000);
    return () => clearTimeout(dismissTimer);
  }, [showSubscriptionSuccess, refreshBilling, searchParams]);

  // Post-credit-checkout return. Stripe sends users back with
  // ?credits=success (or =cancel) when they finish the credit top-up
  // flow. We just strip the param — the credit balance refresh happens
  // automatically via useCredits' refreshInterval/onFocus, and the
  // ProvisioningStepper re-evaluates needsProviderStep on URL change so
  // the wizard advances out of CreditsStep on success.
  // Match the RETURN markers by value, not merely "the param exists".
  //
  // `?credits=bundled` is a different kind of thing: not a marker that a
  // credit Checkout just finished, but a live instruction to CreditsStep —
  // "this balance was already paid for in the subscription Checkout, show the
  // waiting state instead of a purchase form" — that must survive for as long
  // as that step is mounted. Deleting on `!== null` stripped it on the first
  // render, so `awaitingBundledGrant` was never true and the owner who had
  // just paid $150 was shown a live second purchase form anyway. Verified on
  // dev: the browser landed on /chat?provider=bedrock_claude with both
  // `checkout` and `credits` already gone.
  const creditsParam = searchParams.get("credits");
  const cameFromCreditsCheckout = creditsParam === "success" || creditsParam === "cancel";
  useEffect(() => {
    if (!cameFromCreditsCheckout) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("credits");
    replaceSearchParams(params);
  }, [cameFromCreditsCheckout, searchParams]);

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
