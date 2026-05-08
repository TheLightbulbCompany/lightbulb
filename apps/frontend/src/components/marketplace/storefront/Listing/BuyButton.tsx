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
 *   - Signed-in: POST /listings/{slug}/deploy → /deploy-success?listing_slug=…&agent_uuid=…
 *
 * Paid listings → "Buy for $X.XX":
 *   - Signed-out: redirect to Clerk sign-in.
 *   - Signed-in: POST /api/internal/checkout (Stripe Checkout session) → window.location to
 *     the Stripe-hosted URL. Stripe redirects back to /deploy-success on completion,
 *     where the deploy is fired from the post-purchase page.
 */
export function BuyButton({ listing }: { listing: Listing }) {
  const { isSignedIn, getToken } = useAuth();
  const { user } = useUser();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (listing.price_cents === 0) {
    return (
      <FreeDeployButton
        slug={listing.slug}
        isSignedIn={!!isSignedIn}
        getToken={getToken}
        loading={loading}
        setLoading={setLoading}
        error={error}
        setError={setError}
      />
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
    />
  );
}

type GetToken = () => Promise<string | null>;

interface FreeDeployButtonProps {
  slug: string;
  isSignedIn: boolean;
  getToken: GetToken;
  loading: boolean;
  setLoading: (v: boolean) => void;
  error: string | null;
  setError: (v: string | null) => void;
}

function FreeDeployButton({
  slug,
  isSignedIn,
  getToken,
  loading,
  setLoading,
  error,
  setError,
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
        `${API}/marketplace/listings/${encodeURIComponent(slug)}/deploy`,
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
        throw new Error(body?.detail ?? `deploy failed (${resp.status})`);
      }
      const body = (await resp.json()) as { agent_uuid?: string };
      const params = new URLSearchParams({ listing_slug: slug });
      if (body.agent_uuid) params.set("agent_uuid", body.agent_uuid);
      window.location.href = `/deploy-success?${params.toString()}`;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Deploy failed.");
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
        {loading ? "Deploying…" : "Deploy to my Isol8 container"}
      </button>
      {error && (
        <p className="mt-3 rounded-lg border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] px-3 py-2 text-sm text-[var(--danger)]" role="alert">
          {error}{" "}
          <button
            onClick={deploy}
            className="font-bold underline hover:opacity-80"
            type="button"
          >
            Try again
          </button>
        </p>
      )}
    </div>
  );
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
          listing_slug: listing.slug,
          jwt,
          email,
        }),
      });
      const body = await resp.json();
      if (body.checkout_url) {
        window.location.href = body.checkout_url;
        return;
      }
      throw new Error(body?.error ?? "Checkout failed");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Checkout failed.");
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
        <p className="mt-3 rounded-lg border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] px-3 py-2 text-sm text-[var(--danger)]" role="alert">
          {error}{" "}
          <button
            onClick={purchase}
            className="font-bold underline hover:opacity-80"
            type="button"
          >
            Try again
          </button>
        </p>
      )}
    </div>
  );
}
