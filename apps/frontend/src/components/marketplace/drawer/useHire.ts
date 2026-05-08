"use client";

import { useState, useCallback } from "react";
import { useAuth, useUser } from "@clerk/nextjs";

import { useCatalog } from "@/hooks/useCatalog";
import { useAgents } from "@/hooks/useAgents";
import { checkout } from "@/lib/marketplace/api";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

/**
 * Drives the "hire" CTA in the marketplace drawer.
 *
 * Free listings (price_cents === 0):
 *   - POST /marketplace/listings/{slug}/deploy via useCatalog().deploy()
 *   - Mark the resulting agent as freshly installed in the drawer state
 *     (so the chat sidebar can highlight it)
 *   - Show the install toast and auto-close the drawer after 700ms
 *
 * Paid listings (price_cents > 0):
 *   - POST /marketplace/checkout via lib/marketplace/api.checkout()
 *   - Same-tab redirect to the returned Stripe checkout URL
 *
 * The `agent_id` field on DeployResult is what drives both the toast and
 * the freshly-installed marker. If a deploy ever returns no agent id (it
 * shouldn't in practice, but the guard is defensive), we fall back to
 * the slug for toast display only — the freshly-installed setter is
 * skipped because the chat sidebar keys off real agent ids.
 */
export function useHire() {
  const { deploy } = useCatalog();
  const { refresh: refreshAgents } = useAgents();
  const { close, showInstallToast, setFreshlyInstalled } = useMarketplaceDrawer();
  const { getToken } = useAuth();
  const { user } = useUser();
  const [isHiring, setIsHiring] = useState(false);

  const hire = useCallback(
    async (listing: Listing) => {
      setIsHiring(true);
      try {
        if (listing.price_cents === 0) {
          const result = await deploy(listing.slug);
          // Refresh the chat sidebar's agents.list cache BEFORE marking
          // freshly-installed, so the slide-in animation has a row to attach to.
          // useCatalog().deploy() only revalidates the marketplace catalog cache;
          // the sidebar reads useAgents() (a separate agents.list RPC cache).
          await refreshAgents();
          if (result.agent_id) {
            setFreshlyInstalled(result.agent_id);
          }
          showInstallToast({
            agentName: listing.name,
            agentId: result.agent_id ?? listing.slug,
          });
          // 700ms delay before closing matches the mock spec.
          setTimeout(() => close(), 700);
        } else {
          const jwt = await getToken();
          if (!jwt) throw new Error("Not authenticated");
          const result = await checkout({
            listingSlug: listing.slug,
            // {CHECKOUT_SESSION_ID} is a Stripe placeholder — Stripe substitutes
            // the actual session_id into the redirect URL. /deploy-success requires
            // session_id in its query params or its paid-return deploy effect exits
            // and the purchased listing is never installed.
            successUrl: `${window.location.origin}/marketplace/deploy-success?listing_slug=${encodeURIComponent(listing.slug)}&session_id={CHECKOUT_SESSION_ID}`,
            cancelUrl: `${window.location.origin}/chat`,
            jwt,
            email: user?.primaryEmailAddress?.emailAddress,
          });
          window.location.href = result.checkout_url;
        }
      } finally {
        setIsHiring(false);
      }
    },
    [deploy, refreshAgents, close, showInstallToast, setFreshlyInstalled, getToken, user],
  );

  return { hire, isHiring };
}
