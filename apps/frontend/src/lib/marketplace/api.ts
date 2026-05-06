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
