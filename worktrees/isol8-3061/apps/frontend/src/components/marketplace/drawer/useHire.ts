"use client";

import { useState, useCallback } from "react";
import { useAuth, useUser } from "@clerk/nextjs";

import { useCatalog } from "@/hooks/useCatalog";
import { useAgents } from "@/hooks/useAgents";
import { ApiError } from "@/lib/api";
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
    // `isRetry` is internal — set only by this hook's own deploy_busy
    // auto-retry below, never by a caller. Bounds the retry to one attempt.
    async (listing: Listing, reportsTo?: string | null, isRetry = false) => {
      setIsHiring(true);
      try {
        // The deploy path — shared by free listings, seller-self-installs,
        // and paid listings the buyer already owns (409 already_entitled from
        // checkout: never send an entitled buyer back to Stripe).
        const deployNow = async () => {
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
            // The deploy arms no webhook automation — each binds a resource
            // only the buyer can name — so the hire has to SAY what's left,
            // or the agent arrives looking complete and never fires.
            automationsPending: result.automations_pending?.length,
          });
          // 700ms delay before closing matches the mock spec.
          setTimeout(() => close(), 700);
        };

        // Sellers can install their own listing without paying. The
        // backend has a matching seller-self-deploy bypass (see
        // routers/marketplace_listings.py:deploy_listing), so we skip
        // Stripe checkout and call /deploy directly — same path as a
        // free listing — to avoid charging the seller for their own
        // product.
        const isSeller = !!user?.id && listing.seller_id === user.id;
        if (listing.price_cents === 0 || isSeller) {
          await deployNow();
        } else {
          const jwt = await getToken();
          if (!jwt) throw new Error("Not authenticated");
          try {
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
          } catch (checkoutErr) {
            const ce = checkoutErr as { detail?: { error?: string } | string };
            const ceError = typeof ce.detail === "object" ? ce.detail?.error : undefined;
            if (ceError !== "already_entitled") throw checkoutErr;
            await deployNow();
          }
        }
      } catch (err) {
        // A failed deploy/checkout used to escape as an unhandled rejection,
        // leaving the Hire dialog frozen mid-spinner ("the screen bugging
        // out"). Surface it as a toast and keep the dialog open so the buyer
        // can retry — the backend leaves no orphan on failure, so a retry is
        // clean. The drawer is NOT closed here (only the success path closes).
        //
        // The backend's deploy claim is owner-wide (one marketplace deploy in
        // flight per owner) and returns TWO distinct 409 shapes for it that
        // need OPPOSITE handling here — see marketplace_listings.py's
        // deploy_listing docstring:
        const detail =
          err instanceof ApiError && err.status === 409
            ? (err.body as { detail?: { error?: string; retry_after_s?: number } | string } | null)
                ?.detail
            : undefined;

        // The agent-budget refusal is a 409 whose detail is a plain STRING (the
        // sentence to show), not the {error} object the deploy-claim 409s use.
        // It must be handled before those, and it must NOT reach the generic
        // "please try again" tail: retrying is exactly what can never work
        // here, which is the same wall the missing_credentials branch below
        // exists to remove.
        if (typeof detail === "string") {
          showErrorToast(detail);
          return;
        }

        // deploy_in_progress: the buyer's OWN hire of THIS listing is still
        // running. "Couldn't hire… try again" would be the same lie this
        // branch exists to remove — and "try again" is the worst possible
        // advice, since retrying is what produced a duplicate agent on prod.
        if (detail?.error === "deploy_in_progress") {
          showErrorToast(`${listing.name} is already installing — give it a moment.`);
          return;
        }

        // deploy_busy: a DIFFERENT listing of the buyer's is installing.
        // Retrying IS correct here — otherwise this hire never happens — but
        // a manual re-click just gets refused again while the fence is held,
        // so auto-retry once after the server's retry_after_s instead of
        // leaving the buyer at a dead end. `isRetry` bounds it to one attempt;
        // a second refusal falls through to the generic message below rather
        // than retrying forever.
        if (detail?.error === "deploy_busy" && !isRetry) {
          const retryAfterS = typeof detail.retry_after_s === "number" ? detail.retry_after_s : 20;
          showErrorToast(`${listing.name}: your container is busy — retrying shortly…`, { sticky: true });
          setTimeout(() => {
            void hire(listing, reportsTo, true);
          }, retryAfterS * 1000);
          return;
        }

        // missing_credentials: retrying can never succeed until the buyer
        // connects the named providers — the backend sends the list
        // specifically so we can say which (the generic "try again" copy left
        // buyers retrying into the same wall).
        if (detail?.error === "missing_credentials") {
          const missing = ((detail as { missing?: string[] }).missing ?? []).join(", ");
          showErrorToast(
            missing
              ? `${listing.name} needs ${missing} connected — head to Settings → Integrations first.`
              : `${listing.name} needs an integration connected — head to Settings → Integrations first.`,
          );
          return;
        }

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
