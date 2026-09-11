"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useState } from "react";

import { companyMeta } from "@/components/marketplace/storefront/Listing/CompanyCard";
import { deployCompany } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

/**
 * Company-listing CTA on the buyer detail page. Owns the whole company action:
 * an enabled "Deploy company" button that POSTs the deploy (fire-and-forget:
 * the backend returns 202 immediately and instantiates the whole org off the
 * request path) and swaps in a static confirmation card pointing the buyer at
 * Teams.
 *
 * The deploy is fire-and-forget — there is no job to poll. The agents appear in
 * the buyer's Teams roster as they're created (the Paperclip roster is the
 * source of truth), and routines land paused for the buyer to enable from
 * Teams.
 */
export function DeployCompanyButton({ listing }: { listing: Listing }) {
  const company = companyMeta(listing);
  const { isSignedIn, getToken } = useAuth();
  const [started, setStarted] = useState<{ agentCount: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Not a company row — nothing to render (the page only mounts this in the
  // company branch, but stay defensive).
  if (!company) return null;

  if (started) {
    return (
      <section className="editorial-card p-6" data-testid="deploy-started-card" role="status">
        <div className="font-mono text-2xs font-semibold uppercase tracking-caps text-[var(--midnight-muted)]">
          Deploy started
        </div>
        <h3 className="mt-2 font-editorial-serif text-2xl text-[var(--midnight)]">
          Deploying {company.name}
        </h3>
        <p className="mt-3 text-sm leading-relaxed text-[var(--midnight-soft)]">
          Building {started.agentCount} {started.agentCount === 1 ? "agent" : "agents"}. They&apos;ll
          appear in your Teams roster in about a minute — enable their routines
          from Teams once they&apos;re up.
        </p>
        <Link
          href="/chat"
          className="mt-5 inline-block font-mono text-2xs font-semibold uppercase tracking-caps text-[var(--midnight)] underline"
        >
          Open Teams
        </Link>
      </section>
    );
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
      const { agent_count } = await deployCompany({ listingId: listing.listing_id, jwt });
      setStarted({ agentCount: agent_count });
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
        className="editorial-btn editorial-btn--primary h-12 px-6 text-sm disabled:opacity-50"
      >
        {loading ? "Deploying…" : "Deploy company"}
      </button>
      {error && (
        <p
          className="mt-3 rounded-lg border border-[var(--err)] px-3 py-2 text-sm text-[var(--err)]"
          role="alert"
        >
          {error}{" "}
          {/open the app/i.test(error) ? (
            <Link href="/chat" className="font-bold underline hover:opacity-80">
              Open the app
            </Link>
          ) : /already deployed/i.test(error) ? null : (
            <button onClick={deploy} className="font-bold underline hover:opacity-80" type="button">
              Try again
            </button>
          )}
        </p>
      )}
    </div>
  );
}
