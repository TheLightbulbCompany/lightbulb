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
 *   - POST /marketplace/listings/{listing_id}/deploy via useCatalog().deploy()
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
 * the listing_id for toast display only — the freshly-installed setter is
 * skipped because the chat sidebar keys off real agent ids.
 */
export function useHire() {
  const { deploy } = useCatalog();
  const { refresh: refreshAgents } = useAgents();
  const { close, showInstallToast, showErrorToast, setFreshlyInstalled, markInstalled } =
    useMarketplaceDrawer();
  const { getToken } = useAuth();
  const { user } = useUser();
  const [isHiring, setIsHiring] = useState(false);

  const hire = useCallback(
    async (listing: Listing, reportsTo?: string | null) => {
      setIsHiring(true);
      try {
        // Sellers can install their own listing without paying. The
        // backend has a matching seller-self-deploy bypass (see
        // routers/marketplace_listings.py:deploy_listing), so we skip
        // Stripe checkout and call /deploy directly — same path as a
        // free listing — to avoid charging the seller for their own
        // product.
        const isSeller = !!user?.id && listing.seller_id === user.id;
        if (listing.price_cents === 0 || isSeller) {
          // Org placement: thread the buyer's chosen manager into the deploy.
          // (Paid listings deploy post-Stripe-return — reports_to support on
          // that path is a follow-up; all current listings are free.)
          const result = await deploy(listing.listing_id, reportsTo);
          // Flip this listing's card to "Hired ✓" immediately. The catalog's
          // own installedSlugs lags the deploy (sidecar → chokidar reload), so
          // without this optimistic mark a buyer who re-opens the drawer right
          // after hiring still sees "Hire" and can stack a duplicate install.
          markInstalled(listing.slug);
          // Refresh the chat sidebar's agents.list cache BEFORE marking
          // freshly-installed, so the slide-in animation has a row to attach to.
          // useCatalog().deploy() only revalidates the marketplace catalog cache;
          // the sidebar reads useAgents() (a separate agents.list RPC cache).
          await refreshAgents();
          if (result.agent_id) {
            setFreshlyInstalled(result.agent_id);
          }
          // Surface the ACP coding-harness delegation outcome under the
          // install toast. Only non-ACP listings (acp_requested falsy) get no
          // note — those keep the unchanged install-only toast.
          const acpNote = result.acp_requested
            ? result.acp_enabled
              ? "enabled"
              : "unavailable"
            : undefined;
          showInstallToast({
            agentName: listing.name,
            agentId: result.agent_id ?? listing.listing_id,
            acpNote,
          });
          // 700ms delay before closing matches the mock spec.
          setTimeout(() => close(), 700);
        } else {
          const jwt = await getToken();
          if (!jwt) throw new Error("Not authenticated");
          const result = await checkout({
            listingId: listing.listing_id,
            // {CHECKOUT_SESSION_ID} is a Stripe placeholder — Stripe substitutes
            // the actual session_id into the redirect URL. /deploy-success requires
            // session_id in its query params or its paid-return deploy effect exits
            // and the purchased listing is never installed.
            successUrl: `${window.location.origin}/marketplace/deploy-success?listing_id=${encodeURIComponent(listing.listing_id)}&session_id={CHECKOUT_SESSION_ID}`,
            cancelUrl: `${window.location.origin}/chat`,
            jwt,
            email: user?.primaryEmailAddress?.emailAddress,
          });
          window.location.href = result.checkout_url;
        }
      } catch {
        // A failed deploy/checkout used to escape as an unhandled rejection,
        // leaving the Hire dialog frozen mid-spinner ("the screen bugging
        // out"). Surface it as a toast and keep the dialog open so the buyer
        // can retry — the backend leaves no orphan on failure, so a retry is
        // clean. The drawer is NOT closed here (only the success path closes).
        showErrorToast(`Couldn't hire ${listing.name}. Please try again.`);
      } finally {
        setIsHiring(false);
      }
    },
    [
      deploy,
      refreshAgents,
      close,
      showInstallToast,
      showErrorToast,
      setFreshlyInstalled,
      markInstalled,
      getToken,
      user,
    ],
  );

  return { hire, isHiring };
}
