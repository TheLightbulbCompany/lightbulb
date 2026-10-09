"use client";

import type { Listing } from "@/lib/marketplace/types";
import { useAuth, useUser } from "@clerk/nextjs";
import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

/**
 * One-click deploy / buy button for a listing.
 *
 * Free listings (price_cents === 0) → "Deploy to my Isol8 container":
 *   - Signed-out: clicking redirects to Clerk sign-in then back to this listing.
 *   - Signed-in: POST /listings/{listing_id}/deploy →
 *     /marketplace/deploy-success?listing_id=…&agent_uuid=…
 *
 * Paid listings → "Buy for $X.XX":
 *   - Signed-out: redirect to Clerk sign-in.
 *   - Signed-in: POST /api/internal/marketplace/checkout (Stripe Checkout
 *     session) → window.location to the Stripe-hosted URL. Stripe redirects
 *     back to /marketplace/deploy-success on completion, where the deploy is
 *     fired from the post-purchase page.
 *   - Already entitled (409 already_entitled): skip Stripe, go straight to
 *     /marketplace/deploy-success?entitled=1 which runs the deploy.
 */
export function BuyButton({ listing }: { listing: Listing }) {
  const { isSignedIn, getToken, userId, orgId } = useAuth();
  const { user } = useUser();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(true);

  const freeDeploy = (
    <FreeDeployButton
      listingId={listing.listing_id}
      isSignedIn={!!isSignedIn}
      getToken={getToken}
      loading={loading}
      setLoading={setLoading}
      error={error}
      setError={setError}
      retryable={retryable}
      setRetryable={setRetryable}
    />
  );

  if (listing.price_cents === 0) return freeDeploy;

  // A seller is never charged for their own listing (checkout 409s it); the
  // deploy route lets them deploy it free. seller_id is the publishing user
  // or, for a workspace listing, the workspace id.
  if (listing.seller_id && (listing.seller_id === userId || listing.seller_id === orgId)) {
    return (
      <div>
        {freeDeploy}
        <p className="mt-2 text-sm text-[var(--midnight-soft)]">This is your listing</p>
      </div>
    );
  }

  return (
    <PaidBuyButton
      listing={listing}
      isSignedIn={!!isSignedIn}
      getToken={getToken}
      email={user?.primaryEmailAddress?.emailAddress}
      loading={loading}
      setLoading={setLoading}
      error={error}
      setError={setError}
      retryable={retryable}
      setRetryable={setRetryable}
    />
  );
}

type GetToken = () => Promise<string | null>;

interface FreeDeployButtonProps {
  listingId: string;
  isSignedIn: boolean;
  getToken: GetToken;
  loading: boolean;
  setLoading: (v: boolean) => void;
  error: string | null;
  setError: (v: string | null) => void;
  retryable: boolean;
  setRetryable: (v: boolean) => void;
}

function FreeDeployButton({
  listingId,
  isSignedIn,
  getToken,
  loading,
  setLoading,
  error,
  setError,
  retryable,
  setRetryable,
}: FreeDeployButtonProps) {
  async function deploy() {
    setError(null);
    if (!isSignedIn) {
      window.location.href = `/sign-in?redirect_url=${encodeURIComponent(window.location.href)}`;
      return;
    }
    setLoading(true);
    try {
      const jwt = await getToken();
      const resp = await fetch(
        `${API}/marketplace/listings/${encodeURIComponent(listingId)}/deploy`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            Authorization: `Bearer ${jwt}`,
          },
        }
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw structuredDeployError(body?.detail, resp.status);
      }
      const body = (await resp.json()) as { agent_uuid?: string; automations_pending?: unknown[] };
      const params = new URLSearchParams({ listing_id: listingId });
      if (body.agent_uuid) params.set("agent_uuid", body.agent_uuid);
      // The deploy already ran here, so the success page won't re-run it and
      // can't learn the count on its own — carry it, or a storefront buyer
      // never hears that their agent still needs wiring.
      if (body.automations_pending?.length) params.set("pending", String(body.automations_pending.length));
      window.location.href = `/marketplace/deploy-success?${params.toString()}`;
    } catch (e) {
      const de = e as DeployError;
      setError(de.message || "Deploy failed.");
      setRetryable(de.retryable !== false);
      setLoading(false);
    }
  }

  return (
    <div>
      <button
        onClick={deploy}
        disabled={loading}
        className="editorial-btn editorial-btn--primary h-12 px-6 text-sm"
      >
        {loading ? "Deploying…" : "Deploy to my Lightbulb container"}
      </button>
      {error && (
        <p className="mt-3 rounded-lg border border-[var(--err)] px-3 py-2 text-sm text-[var(--err)]" role="alert">
          {error}{" "}
          {retryable && (
            <button
              onClick={deploy}
              className="font-bold underline hover:opacity-80"
              type="button"
            >
              Try again
            </button>
          )}
        </p>
      )}
    </div>
  );
}

type DeployError = Error & { retryable?: boolean };

/**
 * Map the deploy endpoint's structured 409 shapes to human copy. Rendering
 * `body.detail` raw printed "[object Object]", and offering "Try again" on
 * deploy_in_progress is the exact retry-invitation that once duplicated an
 * agent in prod — those come back non-retryable.
 */
function structuredDeployError(detail: unknown, status: number): DeployError {
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; missing?: string[]; retry_after_s?: number };
    if (d.error === "deploy_in_progress") {
      return Object.assign(new Error("This agent is already installing — give it a moment, then check your workspace."), {
        retryable: false,
      });
    }
    if (d.error === "deploy_busy") {
      return Object.assign(new Error("Your container is busy with another install — wait a moment and try again."), {
        retryable: true,
      });
    }
    if (d.error === "missing_credentials") {
      const missing = (d.missing ?? []).join(", ");
      return Object.assign(
        new Error(
          missing
            ? `Connect ${missing} in Settings → Integrations first, then deploy again.`
            : "Connect the required integrations in Settings first, then deploy again.",
        ),
        { retryable: false },
      );
    }
    return Object.assign(new Error(d.error ?? `deploy failed (${status})`), { retryable: true });
  }
  return Object.assign(new Error(typeof detail === "string" ? detail : `deploy failed (${status})`), {
    retryable: true,
  });
}

interface PaidBuyButtonProps {
  listing: Listing;
  isSignedIn: boolean;
  getToken: GetToken;
  email: string | undefined;
  loading: boolean;
  setLoading: (v: boolean) => void;
  error: string | null;
  setError: (v: string | null) => void;
  retryable: boolean;
  setRetryable: (v: boolean) => void;
}

function PaidBuyButton({
  listing,
  isSignedIn,
  getToken,
  email,
  loading,
  setLoading,
  error,
  setError,
  retryable,
  setRetryable,
}: PaidBuyButtonProps) {
  async function purchase() {
    setError(null);
    if (!isSignedIn) {
      window.location.href = `/sign-in?redirect_url=${encodeURIComponent(window.location.href)}`;
      return;
    }
    setLoading(true);
    try {
      const jwt = await getToken();
      const resp = await fetch("/api/internal/marketplace/checkout", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          listing_id: listing.listing_id,
          jwt,
          email,
        }),
      });
      const body = await resp.json();
      if (body.checkout_url) {
        window.location.href = body.checkout_url;
        return;
      }
      // Already entitled: this buyer paid before (e.g. the old broken return
      // URL ate their redirect). Never re-charge — run the deploy instead.
      const detailError =
        body?.detail && typeof body.detail === "object" ? (body.detail as { error?: string }).error : undefined;
      if (detailError === "already_entitled") {
        window.location.href = `/marketplace/deploy-success?listing_id=${encodeURIComponent(listing.listing_id)}&entitled=1`;
        return;
      }
      throw new Error(body?.error ?? "Checkout failed");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Checkout failed.");
      setRetryable(true);
      setLoading(false);
    }
  }

  const price = `$${(listing.price_cents / 100).toFixed(2)}`;
  return (
    <div>
      <button
        onClick={purchase}
        disabled={loading}
        className="editorial-btn editorial-btn--primary h-12 px-6 text-sm"
      >
        {loading ? "Loading…" : `Buy for ${price}`}
      </button>
      {error && (
        <p className="mt-3 rounded-lg border border-[var(--err)] px-3 py-2 text-sm text-[var(--err)]" role="alert">
          {error}{" "}
          {retryable && (
            <button
              onClick={purchase}
              className="font-bold underline hover:opacity-80"
              type="button"
            >
              Try again
            </button>
          )}
        </p>
      )}
    </div>
  );
}
