import type { Listing, ListingDetailResponse } from "@/lib/marketplace/types";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

export async function browseListings(
  opts: { query?: string; limit?: number; format?: "openclaw" | "skillmd" } = {},
): Promise<{ items: Listing[]; count: number }> {
  // The backend search endpoint returns at most `limit` results. v0 storefront
  // only renders the first page, so `count` is just items.length.
  const params = new URLSearchParams();
  if (opts.query) params.set("q", opts.query);
  if (opts.format) params.set("format", opts.format);
  params.set("limit", String(opts.limit ?? 24));

  const resp = await fetch(`${API}/marketplace/listings/search?${params}`, {
    next: { revalidate: 60 },
  });
  if (!resp.ok) throw new Error(`browseListings failed: ${resp.status}`);
  const body = (await resp.json()) as { items: Listing[] };
  return { items: body.items, count: body.items.length };
}

export async function featuredListings(
  opts: { limit?: number } = {},
): Promise<{ items: Listing[] }> {
  // Curator's published listings only (admin@isol8.co). Backend returns an
  // empty list if the curator account hasn't onboarded to Clerk yet — caller
  // renders an empty-state CTA instead of breaking.
  const params = new URLSearchParams();
  params.set("limit", String(opts.limit ?? 6));
  params.set("format", "openclaw");

  const resp = await fetch(`${API}/marketplace/featured-listings?${params}`, {
    next: { revalidate: 60 },
  });
  if (!resp.ok) throw new Error(`featuredListings failed: ${resp.status}`);
  return (await resp.json()) as { items: Listing[] };
}

export async function getListing(slug: string): Promise<ListingDetailResponse | null> {
  const resp = await fetch(`${API}/marketplace/listings/${encodeURIComponent(slug)}`, {
    next: { revalidate: 60 },
  });
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`getListing failed: ${resp.status}`);
  return (await resp.json()) as ListingDetailResponse;
}

export async function checkout(opts: {
  listingSlug: string;
  successUrl: string;
  cancelUrl: string;
  jwt: string;
  email?: string;
}) {
  const resp = await fetch(`${API}/marketplace/checkout`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      Authorization: `Bearer ${opts.jwt}`,
    },
    body: JSON.stringify({
      listing_slug: opts.listingSlug,
      success_url: opts.successUrl,
      cancel_url: opts.cancelUrl,
      email: opts.email,
    }),
  });
  if (!resp.ok) throw new Error(`checkout failed: ${resp.status}`);
  return (await resp.json()) as { checkout_url: string; session_id: string };
}

export interface SearchOptions {
  q?: string;
  category?: string;
  sort?: "installs" | "recent";
  limit?: number;
}

export async function searchListings(
  opts: SearchOptions = {},
): Promise<{ items: Listing[] }> {
  const params = new URLSearchParams();
  if (opts.q) params.set("q", opts.q);
  if (opts.category) params.set("category", opts.category);
  if (opts.sort) params.set("sort", opts.sort);
  params.set("format", "openclaw");
  params.set("limit", String(opts.limit ?? 60));
  const resp = await fetch(`${API}/marketplace/listings/search?${params}`, {
    next: { revalidate: 60 },
  });
  if (!resp.ok) throw new Error(`searchListings failed: ${resp.status}`);
  return (await resp.json()) as { items: Listing[] };
}

export interface ListingUpdate {
  name?: string;
  description_md?: string;
  price_cents?: number;
  tags?: string[];
  category?: string | null;
}

/** Patch metadata on a draft listing. Backend rejects non-draft state
 * with 409 — the wizard surfaces that as a banner.
 *
 * ``version`` is optional and defaults to 1 server-side. Pass the
 * listing's own ``version`` in edit mode so a rejected v_new row (future
 * republish flow) is patched in place instead of clobbering v=1. */
export async function updateListing(opts: {
  listingId: string;
  jwt: string;
  patch: ListingUpdate;
  version?: number;
}): Promise<Listing> {
  const qs = opts.version && opts.version !== 1 ? `?version=${opts.version}` : "";
  const resp = await fetch(
    `${API}/marketplace/listings/${encodeURIComponent(opts.listingId)}${qs}`,
    {
      method: "PATCH",
      headers: {
        "content-type": "application/json",
        Authorization: `Bearer ${opts.jwt}`,
      },
      body: JSON.stringify(opts.patch),
    },
  );
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as { detail?: string };
    throw Object.assign(new Error(body.detail || `updateListing failed: ${resp.status}`), {
      status: resp.status,
      detail: body.detail,
    });
  }
  return (await resp.json()) as Listing;
}

export async function myListings(opts: {
  sellerId: string;
  jwt: string;
  statusIn?: string[];
}): Promise<{ items: Listing[] }> {
  const params = new URLSearchParams();
  params.set("seller_id", opts.sellerId);
  params.set(
    "status_in",
    (opts.statusIn ?? ["draft", "review", "published", "retired", "taken_down"]).join(","),
  );
  params.set("limit", "100");
  const resp = await fetch(`${API}/marketplace/listings/search?${params}`, {
    headers: { Authorization: `Bearer ${opts.jwt}` },
  });
  if (!resp.ok) throw new Error(`myListings failed: ${resp.status}`);
  return (await resp.json()) as { items: Listing[] };
}

// ---------------------------------------------------------------------------
// Publish wizard — artifact preview (Bundle step + Review safety_flags)
// ---------------------------------------------------------------------------

export type SkillBucket = "bundled" | "entries" | "workspace" | "extra_dirs" | "unknown";

export interface AvailableSkill {
  name: string;
  bucket: SkillBucket;
}

export interface SafetyFlag {
  pattern: string;
  severity: "high" | "medium" | "low";
  file: string;
  line: number | null;
  snippet: string;
}

export interface ArtifactPreview {
  agent_id: string;
  available_mcps: string[];
  available_skills: AvailableSkill[];
  persona_files: string[];
  safety_flags: SafetyFlag[];
  needs_config_patch: boolean;
}

export async function artifactPreview(opts: {
  jwt: string;
  agentId: string;
}): Promise<ArtifactPreview> {
  const resp = await fetch(
    `${API}/marketplace/me/agents/${encodeURIComponent(opts.agentId)}/artifact-preview`,
    { headers: { Authorization: `Bearer ${opts.jwt}` } },
  );
  if (!resp.ok) throw new Error(`artifactPreview failed: ${resp.status}`);
  return (await resp.json()) as ArtifactPreview;
}
