import Link from "next/link";

import type { CompanyManifest, Listing } from "@/lib/marketplace/types";

/** Company metadata for `format: "company"` listings; null for agent rows.
 * Gated on format — a stray `company` key on an agent listing's manifest
 * must not switch the detail page into company mode. */
export function companyMeta(listing: Listing): CompanyManifest | null {
  if (listing.format !== "company") return null;
  const company = (listing.manifest_json as { company?: CompanyManifest } | undefined)?.company;
  return company ?? null;
}

export function CompanyCard({ listing }: { listing: Listing }) {
  const company = companyMeta(listing);
  if (!company) return null;
  return (
    <Link
      href={`/marketplace/listing/${listing.listing_id}`}
      className="editorial-card editorial-card-hover group flex flex-col gap-3 p-5 no-underline"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-editorial-serif text-xl text-[var(--midnight)]">{company.name}</h3>
        <span className="rounded-full border border-[var(--edge)] px-2.5 py-1 font-mono text-2xs font-semibold uppercase tracking-caps text-[var(--midnight-muted)]">
          One-click deploy
        </span>
      </div>
      <p className="line-clamp-2 text-sm leading-relaxed text-[var(--midnight-soft)]">{company.description}</p>
      <div className="mt-auto flex flex-wrap gap-x-4 gap-y-1 font-mono text-2xs font-semibold uppercase tracking-caps text-[var(--midnight-muted)]">
        <span>{company.agent_count} agents</span>
        <span>{company.team_count} {company.team_count === 1 ? "team" : "teams"}</span>
        <span>{company.skill_count} skills</span>
        {company.license ? <span>{company.license}</span> : null}
      </div>
    </Link>
  );
}
