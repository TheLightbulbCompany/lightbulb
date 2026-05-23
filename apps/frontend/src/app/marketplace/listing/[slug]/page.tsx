import { notFound } from "next/navigation";

import { getListing } from "@/lib/marketplace/api";
import { BuyButton } from "@/components/marketplace/storefront/Listing/BuyButton";
import { MarkdownDescription } from "@/components/marketplace/storefront/Listing/MarkdownDescription";
import { OwnerManageLink } from "@/components/marketplace/storefront/Listing/OwnerManageLink";
import { Monogram, AgentPill, SectionLabel } from "@/components/agents/AgentIdentity";
import { fromListing } from "@/lib/agentDesign";
import type { ListingManifest } from "@/lib/marketplace/types";

export default async function ListingDetail({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  let detail;
  try {
    detail = await getListing(slug);
  } catch {
    detail = null;
  }
  if (!detail) notFound();
  const { listing, manifest } = detail;
  const editorial = fromListing(listing);
  const isPaid = listing.price_cents > 0;

  return (
    <main className="mx-auto grid max-w-6xl gap-10 px-6 py-12 lg:grid-cols-[360px_1fr]">
      <aside className="lg:sticky lg:top-24 lg:self-start">
        <div className="editorial-card p-7">
          <Monogram agent={editorial} size={96} radius={20} />
          <h1 className="mt-6 font-editorial-serif text-5xl leading-none text-[var(--ink)]">
            {listing.name}
          </h1>
          <p className="mt-2 font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--mute)]">
            {editorial.role}
          </p>
          <p className="mt-5 font-editorial-serif text-lg italic leading-7 text-[var(--forest)]">
            &ldquo;{editorial.vibe}&rdquo;
          </p>
          <div className="mt-6 flex items-center gap-3">
            <AgentPill tone={editorial.isFree ? "forest" : "neutral"}>{editorial.price}</AgentPill>
            {listing.tags.slice(0, 3).map((tag) => (
              <AgentPill key={tag}>{tag}</AgentPill>
            ))}
          </div>
          <div className="mt-6">
            <BuyButton listing={listing} />
          </div>
          {/* Client component — renders the seller-only CTA when the
              signed-in viewer matches listing.seller_id. Server-rendered
              parent stays cacheable and seller-agnostic. */}
          <OwnerManageLink sellerId={listing.seller_id} slug={listing.slug} />
        </div>
      </aside>

      <section className="min-w-0">
        <article className="editorial-card bg-[var(--cream-deeper)] p-8">
          <SectionLabel label="About" />
          <MarkdownDescription source={listing.description_md} />
        </article>

        {manifest && <ManifestSummary manifest={manifest} />}

        {isPaid && (
          <section className="mt-8 editorial-card p-6">
            <SectionLabel label="After purchase" />
            <p className="text-sm leading-7 text-[var(--ink-soft)]">
              After Stripe Checkout completes, your purchased agent deploys to your Isol8 container
              automatically. You&apos;ll be redirected to a confirmation page with a deep link to
              open it in chat.
            </p>
          </section>
        )}
      </section>
    </main>
  );
}

function ManifestSummary({ manifest }: { manifest: ListingManifest }) {
  const sections: Array<{ label: string; items: string[] | undefined }> = [
    { label: "Required skills", items: manifest.required_skills },
    { label: "Required plugins", items: manifest.required_plugins },
    { label: "Required tools", items: manifest.required_tools },
    { label: "Suggested channels", items: manifest.suggested_channels },
  ];

  return (
    <section className="mt-8 editorial-card p-7">
      <SectionLabel label="What's bundled" />
      <div className="grid gap-5 md:grid-cols-2">
        {manifest.vibe && (
          <Field label="Identity">
            <p className="font-editorial-serif text-base italic text-[var(--ink)]">
              &ldquo;{manifest.vibe}&rdquo;
            </p>
          </Field>
        )}
        {manifest.suggested_model && (
          <Field label="Suggested model">
            <p className="font-mono text-sm text-[var(--forest)]">{manifest.suggested_model}</p>
          </Field>
        )}
        {sections.map((section) => (
          <Field key={section.label} label={section.label}>
            {section.items && section.items.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {section.items.map((item) => (
                  <AgentPill key={item}>{item}</AgentPill>
                ))}
              </div>
            ) : (
              <span className="text-sm italic text-[var(--mute)]">None</span>
            )}
          </Field>
        ))}
      </div>
      <p className="mt-6 text-xs leading-5 text-[var(--mute)]">
        Some skills referenced by this agent may not be bundled in the artifact. After deploy,
        your container&apos;s <code className="rounded bg-[var(--cream-deeper)] px-1 py-0.5 font-mono text-[var(--forest)]">openclaw.json</code> may need a manual entry registering the agent.
      </p>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--mute)]">
        {label}
      </div>
      <div>{children}</div>
    </div>
  );
}
