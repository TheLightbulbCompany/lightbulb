import Link from "next/link";

import { listListings } from "@/app/admin/_actions/marketplace";

import { ModerationActions } from "./ModerationActions";

export const metadata = { title: "Listings · Admin" };
// Always re-fetch on navigation: moderators expect fresh queue contents and
// `router.refresh()` after approve/reject must produce up-to-date data.
export const dynamic = "force-dynamic";

interface AdminListing {
  listing_id: string;
  version: number;
  name: string;
  slug: string;
  format: string;
  price_cents: number;
  description_md?: string;
  seller_id: string;
  status: string;
}

/**
 * Status pills shown in the filter bar. The order is the user-facing one
 * (most-actionable → archived). "review" stays the default so moderators
 * landing on the page see the moderation queue without typing anything.
 */
const STATUS_FILTERS: readonly { value: string; label: string }[] = [
  { value: "review", label: "Review" },
  { value: "published", label: "Published" },
  { value: "draft", label: "Drafts" },
  { value: "retired", label: "Retired" },
  { value: "taken_down", label: "Taken down" },
] as const;

const VALID_STATUSES = new Set(STATUS_FILTERS.map((f) => f.value));

/**
 * Listings browse / review surface (Server Component).
 *
 * URL-driven state: `?status=published&q=ray` is the entire UI state, so
 * filters and search are shareable + survive refresh. The filter pills are
 * plain `<Link>`s that swap `?status=` and the search input lives inside a
 * `GET` form posting back to this same page — no client-side state needed.
 *
 * The default status (no `?status=` param) is "review", which preserves the
 * historical "review queue" landing experience for any pre-existing bookmark
 * or link to this page.
 *
 * Each row links to `/admin/marketplace/listings/[id]` (the detail page that
 * already hosts the takedown / approve / reject UI). This page intentionally
 * does NOT add new action buttons beyond the existing moderation actions on
 * review-state rows.
 */
export default async function ListingsBrowse({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; q?: string }>;
}) {
  const params = await searchParams;
  // Coerce unknown statuses to undefined (= treat as the default Review
  // queue) — a typo'd URL renders the queue instead of an error page.
  const explicitStatus =
    params.status && VALID_STATUSES.has(params.status) ? params.status : undefined;
  const q = params.q?.trim() ?? "";
  // ``status`` drives the UI (active pill highlight, heading); when the
  // URL omits it we visually show Review as the active state but DO NOT
  // forward "review" to the backend.
  const status = explicitStatus ?? "review";
  // The backend has two paths for /admin/marketplace/listings:
  //   1. No params → fresh GSI read of status="review" — the moderation
  //      queue with no 60s cache lag.
  //   2. Any of status/q present → marketplace_search.search() (cached).
  // Forward ONLY what the URL actually carries. In particular, when an
  // admin searches from the default queue the URL is `?q=ray` — they
  // want a global search across all statuses, NOT a search scoped to
  // Review. Mapping the absent status back to "review" here would hide
  // published/draft/retired/taken_down matches from that q-only query.
  const result =
    !explicitStatus && !q
      ? await listListings()
      : await listListings({
          ...(explicitStatus ? { status: explicitStatus } : {}),
          ...(q ? { q } : {}),
        });
  if (!result.ok) {
    return (
      <p className="text-sm text-red-400" role="alert">
        Error loading listings: {result.error ?? `http_${result.status}`}
      </p>
    );
  }

  const items =
    (result.data as { items?: AdminListing[] } | undefined)?.items ?? [];

  // Heading reflects the actual scope of the result set: when a user
  // searches from the default Review queue (no explicit status pill but
  // q present), they're searching across all statuses — calling that
  // "Review" matching X would lie about what's in the result set.
  const isGlobalSearch = !explicitStatus && !!q;
  const activeFilter = isGlobalSearch
    ? null
    : STATUS_FILTERS.find((f) => f.value === status);
  const heading = activeFilter ? activeFilter.label : "Listings";

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold text-neutral-100">Listings</h1>

      {/* Filter pills. Each pill is a Link that swaps ?status while preserving
          the current q so a search-then-filter flow doesn't lose the query. */}
      <nav
        aria-label="Filter by status"
        className="flex flex-wrap items-center gap-2"
      >
        {STATUS_FILTERS.map((f) => {
          const isActive = f.value === status;
          const search = new URLSearchParams();
          // Review pill is special: when there's no search query, it's
          // the default queue → omit ?status= so the backend takes the
          // fresh GSI path (no 60s search-cache lag after approve/reject).
          // BUT when there IS a search query, the Review pill must
          // narrow results to status=review like every other pill —
          // otherwise clicking Review during a global "?q=ray" search
          // leaves the URL unchanged and the user sees published/draft
          // matches alongside review ones.
          if (f.value !== "review" || q) search.set("status", f.value);
          if (q) search.set("q", q);
          const qs = search.toString();
          return (
            <Link
              key={f.value}
              href={
                qs
                  ? `/admin/marketplace/listings?${qs}`
                  : `/admin/marketplace/listings`
              }
              aria-current={isActive ? "page" : undefined}
              className={
                isActive
                  ? "rounded-full border border-zinc-100 bg-zinc-100 px-3 py-1 text-xs font-medium text-zinc-900"
                  : "rounded-full border border-zinc-700 bg-zinc-900/40 px-3 py-1 text-xs font-medium text-zinc-300 hover:border-zinc-500 hover:text-zinc-100"
              }
            >
              {f.label}
            </Link>
          );
        })}
      </nav>

      {/* Plain GET form so submission becomes a URL change — no client JS
          required, and the resulting URL is shareable. The hidden status
          input carries the active pill through the submission. */}
      <form
        action="/admin/marketplace/listings"
        method="get"
        className="flex items-center gap-2"
        role="search"
      >
        {/* Only carry status through when it was explicit in the URL.
            On the default queue (no ?status= present) the hidden input
            stays empty so resubmitting a search lands on ?q=... — a
            global all-status search, NOT a review-only one. When the
            user already narrowed to a specific status (incl. an
            explicit ?status=review on a search), preserve it. */}
        {explicitStatus !== undefined && (
          <input type="hidden" name="status" value={explicitStatus} />
        )}
        <input
          type="search"
          name="q"
          defaultValue={q}
          placeholder="Search by name, slug, tag, description…"
          aria-label="Search listings"
          className="w-full rounded-md border border-zinc-700 bg-zinc-900/60 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-zinc-500 focus:outline-none"
        />
        <button
          type="submit"
          className="rounded-md border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm font-medium text-zinc-100 hover:border-zinc-500"
        >
          Search
        </button>
        {q ? (
          <Link
            href={
              explicitStatus !== undefined
                ? `/admin/marketplace/listings?status=${explicitStatus}`
                : `/admin/marketplace/listings`
            }
            className="rounded-md border border-transparent px-2 py-2 text-sm text-zinc-400 hover:text-zinc-200"
          >
            Clear
          </Link>
        ) : null}
      </form>

      <p className="text-sm text-zinc-500">
        {items.length} {heading.toLowerCase()} listing
        {items.length === 1 ? "" : "s"}
        {q ? ` matching "${q}"` : ""}.
      </p>

      {items.length === 0 ? (
        <p className="text-sm text-zinc-400">No listings match these filters.</p>
      ) : (
        <ul className="space-y-3">
          {items.map((listing) => (
            <li
              key={`${listing.listing_id}-${listing.version}`}
              className="flex items-start justify-between gap-4 rounded-lg border border-zinc-800 bg-zinc-900/40 p-5"
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="font-semibold text-zinc-100">
                    <Link
                      href={`/admin/marketplace/listings/${listing.listing_id}?version=${listing.version}`}
                      className="hover:underline"
                    >
                      {listing.name}
                    </Link>
                  </h3>
                  <StatusBadge status={listing.status} />
                </div>
                <p className="mt-1 text-sm text-zinc-400">
                  <code className="font-mono">{listing.slug}</code> ·{" "}
                  {listing.format} · $
                  {(listing.price_cents / 100).toFixed(2)}
                </p>
                {listing.description_md ? (
                  <p className="mt-2 line-clamp-3 text-sm text-zinc-300">
                    {listing.description_md.slice(0, 240)}
                    {listing.description_md.length > 240 ? "…" : ""}
                  </p>
                ) : null}
                <p className="mt-2 text-xs text-zinc-500">
                  seller: <span className="font-mono">{listing.seller_id}</span>
                </p>
                <Link
                  href={`/admin/marketplace/listings/${listing.listing_id}?version=${listing.version}`}
                  className="mt-2 inline-block text-xs text-zinc-300 underline"
                >
                  Open full review →
                </Link>
              </div>
              {/* Approve/Reject only make sense while the listing is in
                  the review queue (backend gates both on status='review' —
                  showing them on other rows just produces confusing 409s).
                  Takedown lives on the detail page so we don't duplicate
                  it here for non-review rows; the "Open full review →"
                  link gets the admin there in one click. */}
              {listing.status === "review" ? (
                <ModerationActions
                  listingId={listing.listing_id}
                  listingName={listing.name}
                  slug={listing.slug}
                  version={listing.version ?? 1}
                />
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Small color-coded chip so a moderator scanning a mixed-status list can
 * triage by color before reading the slug. Colors stay within the dashboard
 * palette to avoid the "AI rainbow" look — review is the only one that pops.
 */
function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    draft: "border-zinc-600 bg-zinc-800/60 text-zinc-300",
    review: "border-amber-500/50 bg-amber-500/10 text-amber-300",
    published: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    retired: "border-zinc-600 bg-zinc-800/60 text-zinc-400",
    taken_down: "border-red-500/40 bg-red-500/10 text-red-300",
  };
  const className =
    styles[status] ?? "border-zinc-600 bg-zinc-800/60 text-zinc-300";
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${className}`}
    >
      {status.replace("_", " ")}
    </span>
  );
}
