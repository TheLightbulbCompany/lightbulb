"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { myListings } from "@/lib/marketplace/api";
import { useApi } from "@/lib/api";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

function formatRejectedAt(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

interface PayoutsDashboard {
  balance_held_cents?: number;
  lifetime_earned_cents?: number;
  dashboard_url?: string | null;
}

export function MyListingsView() {
  const { userId, getToken } = useAuth();
  const api = useApi();
  const { setView, startEditListing } = useMarketplaceDrawer();
  const [listings, setListings] = useState<Listing[] | null>(null);
  const [dashboard, setDashboard] = useState<PayoutsDashboard | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
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
    // the omission is benign.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  if (!listings || !dashboard) {
    return <p className="font-editorial-serif italic text-[#8a8578]">Loading…</p>;
  }

  const activeCount = listings.filter((l) => l.status === "published").length;
  const totalInstalls = listings.reduce(
    (s, l) => s + ((l as unknown as { install_count?: number }).install_count ?? 0),
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

      {listings.length === 0 ? (
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
          {listings.map((l) => (
            <ListingRow
              key={l.listing_id}
              listing={l}
              onEdit={() => startEditListing(l)}
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

function ListingRow({ listing, onEdit }: { listing: Listing; onEdit: () => void }) {
  const installCount = (listing as unknown as { install_count?: number }).install_count ?? 0;
  // A draft with rejection_notes is the post-rejection state — the seller
  // submitted, the admin sent it back. Frontend doesn't get a distinct
  // status; presence of rejection_notes is the signal.
  const wasRejected = listing.status === "draft" && !!listing.rejection_notes;
  const rejectedOn = wasRejected ? formatRejectedAt(listing.rejected_at) : null;
  const isEditable = listing.status === "draft";

  return (
    <div
      data-testid="my-listings-row"
      data-was-rejected={wasRejected ? "true" : undefined}
      className={`bg-white border rounded-2xl p-5 flex items-start gap-4 ${
        wasRejected ? "border-[#f0b3a8] bg-[#fff8f5]" : "border-[#e0dbd0]"
      }`}
    >
      <div className="flex-1 min-w-0">
        <div className="font-editorial-serif text-lg">{listing.name}</div>
        <div className="flex gap-3 text-[12.5px] text-[#5b5b5b] mt-0.5">
          <span>Updated {new Date(listing.created_at).toLocaleDateString()}</span>
          <span>{installCount} installs</span>
          <span className="font-semibold text-[#06402b]">
            {listing.price_cents === 0 ? "Free" : "Premium tier"}
          </span>
        </div>
        {wasRejected && (
          <div
            role="status"
            data-testid="rejection-notes"
            className="mt-2.5 rounded-xl border border-[#f0b3a8] bg-[#fbe9e7] px-3 py-2 text-[12.5px] text-[#8a3a2a] leading-snug"
          >
            <strong className="font-semibold">
              Rejected{rejectedOn ? ` · ${rejectedOn}` : ""}.
            </strong>{" "}
            {listing.rejection_notes}
          </div>
        )}
      </div>
      <div className="flex flex-col items-end gap-2 flex-shrink-0">
        <span
          className={`text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full whitespace-nowrap ${
            listing.status === "published"
              ? "bg-[#e3efe6] text-[#06402b]"
              : listing.status === "review"
                ? "bg-[#fef4e3] text-[#a07520]"
                : wasRejected
                  ? "bg-[#fbe9e7] text-[#8a3a2a]"
                  : "bg-[#f3eee5] text-[#5b5b5b]"
          }`}
        >
          {listing.status === "published"
            ? "Live"
            : listing.status === "review"
              ? "In review"
              : wasRejected
                ? "Rejected"
                : listing.status}
        </span>
        {isEditable && (
          <button
            type="button"
            onClick={onEdit}
            data-testid="edit-listing"
            className="text-[12.5px] font-semibold text-[#06402b] hover:bg-[#e3efe6] rounded-full px-3 py-1"
          >
            {wasRejected ? "Revise →" : "Edit →"}
          </button>
        )}
      </div>
    </div>
  );
}
