"use client";

import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import Link from "next/link";

import { companyMeta } from "@/components/marketplace/storefront/Listing/CompanyCard";
import { deployCompany } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

/**
 * Company card for the in-workspace marketplace drawer's Companies scope.
 *
 * Same deploy mechanics as the storefront's DeployCompanyButton (fire-and-
 * forget POST → 202 → agents land in the Teams roster; routines arrive
 * PAUSED), restyled to the drawer's card idiom. Plan gating stays
 * server-side: the "Requires Ultra Pro" 409 renders an upgrade link and the
 * pod-not-running 503 tells the owner to wake their agents (in-workspace
 * that just means using the app — the status poll wakes a sleeping pod).
 */
export function CompanyListingCard({ listing }: { listing: Listing }) {
  const company = companyMeta(listing);
  const { getToken } = useAuth();
  const [phase, setPhase] = useState<"idle" | "deploying" | "started">("idle");
  const [error, setError] = useState<string | null>(null);

  if (!company) return null;

  async function deploy() {
    setError(null);
    setPhase("deploying");
    try {
      const jwt = await getToken();
      if (!jwt) throw new Error("Not signed in.");
      await deployCompany({ listingId: listing.listing_id, jwt });
      setPhase("started");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Deploy failed.");
      setPhase("idle");
    }
  }

  return (
    <article
      className={`relative rounded-2xl border p-5 flex flex-col gap-3 min-h-[200px] transition ${
        phase === "started"
          ? "border-[var(--ok)]"
          : "border-[var(--edge)] bg-card hover:-translate-y-0.5 hover:shadow-md hover:border-[var(--midnight-faint)]"
      }`}
    >
      {company.min_plan === "ultra_pro" && (
        <span className="absolute top-3 right-3.5 text-[9.5px] tracking-widest text-[var(--midnight)] font-bold">
          ULTRA PRO
        </span>
      )}
      <Link
        href={`/marketplace/listing/${listing.listing_id}`}
        target="_blank"
        rel="noopener noreferrer"
        className="font-editorial-serif text-lg leading-tight text-[var(--midnight)] no-underline hover:underline"
      >
        {company.name}
      </Link>
      <p className="text-[13px] text-muted-foreground leading-snug line-clamp-2">{company.description}</p>
      <div className="mt-auto flex items-center justify-between pt-2.5 border-t border-dashed border-[var(--edge)]">
        <span className="text-[11.5px] italic text-muted-foreground">
          {company.agent_count} agents · {company.skill_count} skills
        </span>
        {phase === "started" ? (
          <span className="text-[12px] font-semibold text-[var(--midnight)]" role="status">
            Deploying — check Teams
          </span>
        ) : (
          <button
            type="button"
            onClick={deploy}
            disabled={phase === "deploying"}
            className="text-[var(--cream)] bg-[var(--midnight)] border border-[var(--midnight)] px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold hover:opacity-90 transition disabled:opacity-60"
          >
            {phase === "deploying" ? "Deploying…" : "Deploy company"}
          </button>
        )}
      </div>
      {error && (
        <p className="text-[12px] text-[var(--err)]" role="alert">
          {error}{" "}
          {/Ultra Pro/i.test(error) && (
            <Link href="/settings" className="font-bold underline hover:opacity-80">
              Upgrade in Settings
            </Link>
          )}
        </p>
      )}
    </article>
  );
}
