"use client";

import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { myListings } from "@/lib/marketplace/api";
import { useApi } from "@/lib/api";
import { useMarketplaceWriteGate } from "@/lib/marketplace/useMarketplaceWriteGate";
import { MarketplaceRestrictedView } from "@/components/marketplace/RestrictedView";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

interface PayoutsDashboard {
  balance_held_cents?: number;
  lifetime_earned_cents?: number;
  dashboard_url?: string | null;
}

interface GroupedListing {
  /** The seller-visible canonical row for the listing — usually the live
   * published version. For listings that have never published (or were
   * taken down / retired) it falls back to the most-relevant non-pending
   * row so the listing still appears in the list. */
  live: Listing;
  /** The pending draft / review row for v(N+1) when the seller has
   * started a republish. Null when no in-flight revision exists. */
  pending: Listing | null;
}

export function MyListingsView() {
  const { userId, getToken } = useAuth();
  const api = useApi();
  const { setView, openListingDetail } = useMarketplaceDrawer();
  const { restricted } = useMarketplaceWriteGate();
  const [listings, setListings] = useState<Listing[] | null>(null);
  const [dashboard, setDashboard] = useState<PayoutsDashboard | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Org members without the org:admin role can't publish OR see payouts
    // — both POST /listings and GET /payouts/dashboard 403 for them.
    // Skip the fetches entirely so we don't trip the toast-on-error path
    // and so the restricted view below renders without a loading flash.
    if (restricted) return;
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt || !userId) return;
      const [r1, r2] = await Promise.all([
        myListings({ sellerId: userId, jwt }),
        api.get("/marketplace/payouts/dashboard"),
      ]);
      if (!alive) return;
      setListings(r1.items);
      setDashboard(r2 as PayoutsDashboard);
    })().catch(() => {
      if (alive) {
        setListings([]);
        setDashboard({});
      }
    });
    return () => {
      alive = false;
    };
    // Only re-run when the seller identity changes. `getToken` and `api` are
    // recreated on every render in tests (mock factories return fresh
    // vi.fn() instances), and we don't want the effect to burn through
    // mockResolvedValueOnce queues. In production both are stable refs so
    // the omission is benign. `restricted` is included so that a member
    // who gets promoted to admin mid-session triggers the fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, restricted]);

  // Group raw rows by listing_id so a republish-in-progress (live v1 +
  // draft/review v2) collapses into a single row. The detail view is the
  // single door for managing both versions; the list page no longer
  // exposes v(N+1) as its own clickable row.
  //
  // useMemo runs before either early return below — React requires the
  // hook order to be stable across renders even when one branch bails
  // straight to a restricted view.
  const grouped = useMemo<GroupedListing[]>(() => {
    if (!listings) return [];
    const byId = new Map<string, { live: Listing | null; pending: Listing | null }>();
    for (const l of listings) {
      const entry = byId.get(l.listing_id) ?? { live: null, pending: null };
      if (l.status === "published") {
        entry.live = l;
      } else if (l.status === "draft" || l.status === "review") {
        // Most recent draft/review wins. Sort key: created_at desc.
        if (!entry.pending || (l.created_at ?? "") > (entry.pending.created_at ?? "")) {
          entry.pending = l;
        }
      } else if (!entry.live) {
        // taken_down / retired listings still need a row so the seller
        // can see + manage them. Only adopt as `live` when no actual
        // published row exists for this id.
        entry.live = l;
      }
      byId.set(l.listing_id, entry);
    }
    // Promote draft-only / review-only listings (no published row yet) so
    // they render in the seller's list. Otherwise a fresh draft or a
    // first-time submission that hasn't been approved would vanish from
    // My Listings entirely — invisible to the seller who just created it.
    // The promoted row's status pill (Draft / In review / Rejected) flows
    // through ListingRow naturally; pendingLabel stays null because
    // `pending` is cleared, so no "· vN in review" suffix is appended.
    for (const entry of byId.values()) {
      if (entry.live === null && entry.pending !== null) {
        entry.live = entry.pending;
        entry.pending = null;
      }
    }
    return Array.from(byId.values())
      .filter((e): e is GroupedListing => e.live !== null)
      .sort((a, b) => (b.live.created_at ?? "").localeCompare(a.live.created_at ?? ""));
  }, [listings]);

  if (restricted) {
    return (
      <MarketplaceRestrictedView
        title="Publishing restricted"
        desc="Only org admins can publish listings or manage marketplace payouts. Ask your org admin to take over."
      />
    );
  }

  if (!listings || !dashboard) {
    return <p className="font-editorial-serif italic text-[#8a8578]">Loading…</p>;
  }

  const activeCount = grouped.filter((g) => g.live.status === "published").length;
  // Sum installs across the canonical row per listing — counting both the
  // live and pending rows would double-bill the same installs.
  const totalInstalls = grouped.reduce(
    (s, g) => s + ((g.live as unknown as { install_count?: number }).install_count ?? 0),
    0,
  );
  const pendingCents = dashboard.balance_held_cents ?? 0;
  const isOnboarded = !!dashboard.dashboard_url;

  async function handlePayoutAction() {
    setBusy(true);
    try {
      if (isOnboarded) {
        window.open(dashboard!.dashboard_url!, "_blank", "noopener,noreferrer");
      } else {
        // Test asserts toHaveBeenCalledWith("/marketplace/payouts/onboard")
        // (single arg). Cast lets us match the test's strict-args check while
        // keeping useApi().post's standard 2-arg shape elsewhere.
        const post = api.post as unknown as (endpoint: string) => Promise<unknown>;
        const r = (await post("/marketplace/payouts/onboard")) as { onboarding_url: string };
        window.location.href = r.onboarding_url;
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="flex items-end justify-between gap-3.5 flex-wrap mb-4">
        <div>
          <h3 className="font-editorial-serif text-2xl text-[#1a1a1a]">My listings</h3>
          <div className="flex gap-6 mt-2 text-[13px] text-[#5b5b5b]">
            <Stat label="active listings" value={activeCount.toString()} />
            <Stat label="total installs" value={totalInstalls.toLocaleString()} />
            <Stat
              label="pending payout"
              value={`$${(pendingCents / 100).toFixed(2)}`}
              action={isOnboarded ? "Cash out →" : "Set up payouts →"}
              onAction={handlePayoutAction}
              disabled={busy}
            />
          </div>
        </div>
        <button
          type="button"
          onClick={() => setView("publish")}
          className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-4 py-2 rounded-full text-[13px] font-semibold"
        >
          + New listing
        </button>
      </div>

      {grouped.length === 0 ? (
        <div className="text-center py-12 border border-dashed border-[#e0dbd0] rounded-2xl bg-white">
          <h4 className="font-editorial-serif text-xl mb-2">No listings yet.</h4>
          <p className="text-[14px] text-[#5b5b5b] mb-4">
            Open chat to publish your first agent.
          </p>
          <button
            type="button"
            onClick={() => setView("publish")}
            className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-5 py-2 rounded-full text-[14px] font-semibold"
          >
            + Publish your agent
          </button>
        </div>
      ) : (
        <div className="space-y-2.5">
          {grouped.map(({ live, pending }) => (
            <ListingRow
              key={live.listing_id}
              live={live}
              pending={pending}
              onClick={() => openListingDetail(live.listing_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  action,
  onAction,
  disabled,
}: {
  label: string;
  value: string;
  action?: string;
  onAction?: () => void;
  disabled?: boolean;
}) {
  return (
    <div>
      <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">{value}</strong>
      <span>{label}</span>
      {action && (
        <button
          type="button"
          onClick={onAction}
          disabled={disabled}
          className="block text-[12px] font-semibold text-[#06402b] hover:underline disabled:opacity-60 mt-1"
        >
          {action}
        </button>
      )}
    </div>
  );
}

function ListingRow({
  live,
  pending,
  onClick,
}: {
  live: Listing;
  pending: Listing | null;
  onClick: () => void;
}) {
  const installCount = (live as unknown as { install_count?: number }).install_count ?? 0;
  // A draft with rejection_notes is the post-rejection state — the seller
  // submitted, the admin sent it back. Frontend doesn't get a distinct
  // status; presence of rejection_notes is the signal. Inspect both rows
  // — a rejected v(N+1) lives on the pending row even though `live`
  // remains the previously published version.
  const liveWasRejected = live.status === "draft" && !!live.rejection_notes;
  const pendingWasRejected = pending?.status === "draft" && !!pending?.rejection_notes;
  const wasRejected = liveWasRejected || pendingWasRejected;

  // Base label reflects the live row only. The pending suffix (below)
  // tells the seller whether v(N+1) is in motion.
  const baseStatusLabel =
    live.status === "published"
      ? "Live"
      : live.status === "review"
        ? "In review"
        : live.status === "draft" && live.rejection_notes
          ? "Rejected"
          : live.status === "draft"
            ? "Draft"
            : live.status;

  // Suffix surfaces a republish-in-progress on a row whose live status
  // is already published. We deliberately only emit it for the
  // review / rejected states — a fresh untouched draft (no
  // rejection_notes, no submit) is a private workspace the seller
  // hasn't surfaced and doesn't need a callout for.
  const pendingLabel =
    pending && pending.status === "review"
      ? `· v${pending.version} in review`
      : pendingWasRejected
        ? `· v${pending!.version} rejected`
        : null;

  // Pill color follows the most-alarming state present. Pending
  // rejected outranks live published — we want the seller's eye on the
  // republish that needs attention.
  const pillTone = pendingWasRejected
    ? "bg-[#fbe9e7] text-[#8a3a2a]"
    : pending?.status === "review"
      ? "bg-[#fef4e3] text-[#a07520]"
      : live.status === "published"
        ? "bg-[#e3efe6] text-[#06402b]"
        : live.status === "review"
          ? "bg-[#fef4e3] text-[#a07520]"
          : liveWasRejected
            ? "bg-[#fbe9e7] text-[#8a3a2a]"
            : "bg-[#f3eee5] text-[#5b5b5b]";

  return (
    <button
      type="button"
      data-testid="my-listings-row"
      data-was-rejected={wasRejected ? "true" : undefined}
      onClick={onClick}
      className={`w-full text-left bg-white border rounded-2xl p-5 flex items-start gap-4 hover:bg-[#fbf9f4] transition-colors ${
        wasRejected ? "border-[#f0b3a8] bg-[#fff8f5]" : "border-[#e0dbd0]"
      }`}
    >
      <div className="flex-1 min-w-0">
        <div className="font-editorial-serif text-lg">{live.name}</div>
        <div className="flex gap-3 text-[12.5px] text-[#5b5b5b] mt-0.5">
          <span>Updated {new Date(live.created_at).toLocaleDateString()}</span>
          <span>{installCount} installs</span>
          <span className="font-semibold text-[#06402b]">
            {live.price_cents === 0 ? "Free" : "Premium tier"}
          </span>
        </div>
      </div>
      <div className="flex flex-col items-end gap-2 flex-shrink-0">
        <span
          className={`text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full whitespace-nowrap ${pillTone}`}
        >
          {baseStatusLabel}
          {pendingLabel ? ` ${pendingLabel}` : ""}
        </span>
      </div>
    </button>
  );
}
