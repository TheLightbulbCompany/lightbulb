import { notFound } from "next/navigation";

import { getListing } from "@/lib/marketplace/api";
import { BuyButton } from "@/components/marketplace/storefront/Listing/BuyButton";
import { DeployCompanyButton } from "@/components/marketplace/company/DeployCompanyButton";
import { companyMeta } from "@/components/marketplace/storefront/Listing/CompanyCard";
import { MarkdownDescription } from "@/components/marketplace/storefront/Listing/MarkdownDescription";
import { OwnerManageLink } from "@/components/marketplace/storefront/Listing/OwnerManageLink";
import { Monogram, AgentPill, SectionLabel } from "@/components/agents/AgentIdentity";
import { fromListing } from "@/lib/agentDesign";
import type { CompanyManifest, ListingManifest, RequirementRow } from "@/lib/marketplace/types";

export default async function ListingDetail({
  params,
}: {
  params: Promise<{ listingId: string }>;
}) {
  const { listingId } = await params;
  let detail;
  try {
    detail = await getListing(listingId);
  } catch {
    detail = null;
  }
  if (!detail) notFound();
  const { listing, manifest } = detail;
  const editorial = fromListing(listing);
  const company = companyMeta(listing);
  const isPaid = listing.price_cents > 0;

  return (
    <main className="mx-auto grid max-w-6xl gap-10 px-6 py-12 lg:grid-cols-[360px_1fr]">
      <aside className="lg:sticky lg:top-24 lg:self-start">
        <div className="editorial-card p-7">
          <Monogram agent={editorial} size={96} radius={20} />
          <h1 className="mt-6 font-editorial-serif text-5xl leading-none text-[var(--midnight)]">
            {listing.name}
          </h1>
          <p className="mt-2 font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--midnight-muted)]">
            {editorial.role}
          </p>
          <p className="mt-5 font-editorial-serif text-lg italic leading-7 text-[var(--midnight)]">
            &ldquo;{editorial.vibe}&rdquo;
          </p>
          <div className="mt-6 flex items-center gap-3">
            <AgentPill tone={editorial.isFree ? "neutral" : "brass"}>{editorial.price}</AgentPill>
            {listing.tags.slice(0, 3).map((tag) => (
              <AgentPill key={tag}>{tag}</AgentPill>
            ))}
          </div>
          <div className="mt-6">
            {company ? (
              <DeployCompanyButton listing={listing} />
            ) : (
              <BuyButton listing={listing} />
            )}
          </div>
          {/* Client component — renders the seller-only CTA when the
              signed-in viewer matches listing.seller_id. Server-rendered
              parent stays cacheable and seller-agnostic. */}
          <OwnerManageLink sellerId={listing.seller_id} listingId={listing.listing_id} />
        </div>
      </aside>

      <section className="min-w-0">
        <article className="editorial-card bg-[var(--cream-deep)] p-8">
          <SectionLabel label="About" />
          <MarkdownDescription source={listing.description_md} />
        </article>

        {company && <CompanyFacts company={company} />}

        {!company && manifest && <ManifestSummary manifest={manifest} />}

        {isPaid && (
          <section className="mt-8 editorial-card p-6">
            <SectionLabel label="After purchase" />
            <p className="text-sm leading-7 text-[var(--midnight-soft)]">
              After Stripe Checkout completes, your purchased agent deploys to your Lightbulb container
              automatically. You&apos;ll be redirected to a confirmation page with a deep link to
              open it in chat.
            </p>
          </section>
        )}
      </section>
    </main>
  );
}

// Disposition groups rendered in worst-first → best order. Each row's
// `disposition` (from the backend requirements ledger) maps to one labeled
// group; empty groups are skipped.
const REQUIREMENT_GROUPS: Array<{
  disposition: RequirementRow["disposition"];
  label: string;
  hint: string;
}> = [
  {
    disposition: "included",
    label: "Included",
    hint: "Ships with the agent — bundled skills & tools.",
  },
  {
    disposition: "connect",
    label: "Connect an integration",
    hint: "Optional — connect these in Settings.",
  },
  {
    disposition: "byo",
    label: "Bring your own key",
    hint: "Needs an API key you provide.",
  },
  {
    disposition: "unavailable",
    label: "Not available on Lightbulb",
    hint: "Referenced by the agent but not supported here.",
  },
];

function requirementLabel(req: RequirementRow): string {
  const base = req.display_name || req.key;
  // BYO rows surface the env-var name the buyer must supply, e.g.
  // "Parallel · PARALLEL_API_KEY".
  if (req.disposition === "byo" && req.env.length > 0) {
    return `${base} · ${req.env[0]}`;
  }
  return base;
}

export function ManifestSummary({ manifest }: { manifest: ListingManifest }) {
  const requirements = manifest.requirements ?? [];
  const groups = REQUIREMENT_GROUPS.map((group) => ({
    ...group,
    rows: requirements.filter((req) => req.disposition === group.disposition),
  })).filter((group) => group.rows.length > 0);

  return (
    <section className="mt-8 editorial-card p-7">
      <SectionLabel label="What's bundled" />
      <div className="grid gap-5 md:grid-cols-2">
        {manifest.vibe && (
          <Field label="Identity">
            <p className="font-editorial-serif text-base italic text-[var(--midnight)]">
              &ldquo;{manifest.vibe}&rdquo;
            </p>
          </Field>
        )}
        {manifest.suggested_model && (
          <Field label="Suggested model">
            <p className="font-mono text-sm text-[var(--midnight)]">{manifest.suggested_model}</p>
          </Field>
        )}
        {groups.length > 0 ? (
          groups.map((group) => (
            <Field key={group.disposition} label={group.label}>
              <div className="flex flex-wrap gap-1.5">
                {group.rows.map((req) => (
                  <AgentPill key={req.key}>{requirementLabel(req)}</AgentPill>
                ))}
              </div>
              <p className="mt-2 text-xs leading-5 text-[var(--midnight-muted)]">{group.hint}</p>
            </Field>
          ))
        ) : (
          <Field label="Integrations">
            <p className="text-sm italic text-[var(--midnight-muted)]">
              Self-contained — no external integrations required.
            </p>
          </Field>
        )}
      </div>
      {manifest.scan_incomplete === true && (
        <p className="mt-6 text-xs leading-5 text-[var(--midnight-muted)]">
          Some dependencies may not have been fully detected.
        </p>
      )}
    </section>
  );
}

/** Company-listing facts: org-chart counts, env vars the buyer must supply,
 * and the mirrored-from attribution. Rendered instead of ManifestSummary —
 * company metadata rides on the listing row, not the S3 agent manifest. */
function CompanyFacts({ company }: { company: CompanyManifest }) {
  // Defensive: rows written by older ingestion runs may lack these fields.
  const envNames = Array.from(new Set((company.env_requirements ?? []).map((req) => req.name)));
  return (
    <section className="mt-8 editorial-card p-7">
      <SectionLabel label="What's inside" />
      <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] uppercase tracking-[0.12em] text-[var(--midnight-muted)]">
        <span>{company.agent_count} agents</span>
        <span>{company.team_count} {company.team_count === 1 ? "team" : "teams"}</span>
        <span>{company.skill_count} skills</span>
      </div>
      {envNames.length > 0 && (
        <div className="mt-5">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--midnight-muted)]">
            Environment required
          </div>
          <div className="flex flex-wrap gap-1.5">
            {envNames.map((name) => (
              <AgentPill key={name}>{name}</AgentPill>
            ))}
          </div>
        </div>
      )}
      {company.source?.repo && (
        <p className="mt-6 text-xs leading-5 text-[var(--midnight-muted)]">
          Mirrored from {company.source.repo}
          {company.license ? ` · ${company.license}` : ""}
        </p>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--midnight-muted)]">
        {label}
      </div>
      <div>{children}</div>
    </div>
  );
}
