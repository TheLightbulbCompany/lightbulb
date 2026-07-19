"use client";

import { useAuth } from "@clerk/nextjs";
import { useState } from "react";

import { companyMeta } from "@/components/marketplace/storefront/Listing/CompanyCard";
import { deployCompany } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

import { DeployProgress } from "./DeployProgress";

/**
 * Company-listing CTA on the buyer detail page. Owns the whole company action:
 *
 *  - `min_plan: "ultra_pro"` → the "Requires Ultra Pro" upsell (that tier's
 *    deploy engine ships later). No active deploy.
 *  - `min_plan: "standard"`  → an enabled "Deploy company" button. Clicking it
 *    POSTs the deploy, stores the returned job_id, and hands off to
 *    `DeployProgress`.
 *
 * The button renders unconditionally for standard companies — the platform
 * admin-only window is enforced *server-side* (the deploy route requires a
 * platform admin until the dev E2E validates the engine), and a later task
 * opens it to all buyers. A non-admin who clicks it gets the backend's 403
 * surfaced inline rather than a hidden button.
 */
export function DeployCompanyButton({ listing }: { listing: Listing }) {
  const company = companyMeta(listing);
  const { isSignedIn, getToken } = useAuth();
  const [jobId, setJobId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Not a company row — nothing to render (the page only mounts this in the
  // company branch, but stay defensive).
  if (!company) return null;

  if (company.min_plan === "ultra_pro") {
    return (
      <button
        disabled
        className="editorial-btn editorial-btn--primary h-12 px-6 text-sm opacity-60"
        title="Ultra Pro tier coming soon"
      >
        Requires Ultra Pro
      </button>
    );
  }

  if (jobId) {
    return <DeployProgress jobId={jobId} listingId={listing.listing_id} />;
  }

  async function deploy() {
    setError(null);
    if (!isSignedIn) {
      window.location.href = `/sign-in?redirect_url=${encodeURIComponent(window.location.href)}`;
      return;
    }
    setLoading(true);
    try {
      const jwt = await getToken();
      if (!jwt) throw new Error("Not signed in.");
      const { job_id } = await deployCompany({ listingId: listing.listing_id, jwt });
      setJobId(job_id);
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
        className="editorial-btn editorial-btn--primary h-12 px-6 text-sm disabled:opacity-60"
      >
        {loading ? "Deploying…" : "Deploy company"}
      </button>
      {error && (
        <p
          className="mt-3 rounded-lg border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] px-3 py-2 text-sm text-[var(--danger)]"
          role="alert"
        >
          {error}{" "}
          <button onClick={deploy} className="font-bold underline hover:opacity-80" type="button">
            Try again
          </button>
        </p>
      )}
    </div>
  );
}
