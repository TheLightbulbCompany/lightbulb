"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import {
  discardListingVersion,
  myListings,
  republishListing,
} from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";
import { useMarketplaceDrawer } from "./drawer/MarketplaceDrawerProvider";

type Tab = "overview" | "bundle" | "pending";

interface Props {
  listingId: string;
  mode: "drawer" | "page";
}

export function ListingDetailView({ listingId, mode }: Props) {
  const { userId, getToken } = useAuth();
  const [live, setLive] = useState<Listing | null>(null);
  const [pending, setPending] = useState<Listing | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  // Bump to force the fetch effect to re-run after a mutation (discard).
  // Republish hands off to the wizard so doesn't need a local refetch.
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!jwt || !userId) return;
      // Fetch this seller's listings; find the rows for our listing_id. One
      // round-trip surfaces both the live row and any in-flight v(N+1)
      // draft/review — we don't need a per-row GET for either.
      const { items } = await myListings({ sellerId: userId, jwt });
      if (!alive) return;
      const rows = items.filter((l) => l.listing_id === listingId);
      setLive(rows.find((r) => r.status === "published") ?? rows[0] ?? null);
      setPending(
        rows.find((r) => r.status === "draft" || r.status === "review") ?? null,
      );
    })();
    return () => {
      alive = false;
    };
  }, [listingId, userId, getToken, refreshKey]);

  if (!live) {
    return (
      <p className="font-editorial-serif italic text-[#8a8578]">Loading…</p>
    );
  }

  const containerCls =
    mode === "page" ? "max-w-4xl mx-auto px-6 py-8" : "px-2";

  return (
    <div data-mode={mode} className={containerCls}>
      <Header
        live={live}
        pending={pending}
        onPendingChange={() => setRefreshKey((k) => k + 1)}
      />
      <Stats live={live} />
      <Tabs tab={tab} setTab={setTab} showPending={!!pending} />
      {tab === "overview" && <OverviewTab listing={live} />}
      {tab === "bundle" && <BundleTab listing={live} />}
      {tab === "pending" && pending && <PendingTab pending={pending} />}
    </div>
  );
}

function Header({
  live,
  pending,
  onPendingChange,
}: {
  live: Listing;
  pending: Listing | null;
  onPendingChange: () => void;
}) {
  const { getToken } = useAuth();
  const { startEditListing } = useMarketplaceDrawer();
  const [busy, setBusy] = useState(false);

  // Sub-badge surfaces an in-flight v(N+1) on the same pill so the seller
  // can tell at a glance whether they have a republish in motion.
  const pendingBadge =
    pending &&
    (pending.status === "review"
      ? `· v${pending.version} in review`
      : pending.rejection_notes
        ? `· v${pending.version} rejected`
        : `· v${pending.version} draft`);

  async function handleRepublish() {
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) return;
      // Backend clones the live row into a fresh v(N+1) draft. We hand the
      // new row to the publish wizard, which prefills + locks the agent
      // picker and submits via PATCH → artifact → submit.
      const draft = await republishListing({
        listingId: live.listing_id,
        jwt,
      });
      startEditListing(draft);
    } finally {
      setBusy(false);
    }
  }

  async function handleDiscard() {
    if (!pending) return;
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) return;
      await discardListingVersion({
        listingId: live.listing_id,
        version: pending.version,
        jwt,
      });
      onPendingChange();
    } finally {
      setBusy(false);
    }
  }

  // Republish requires a Live (published) row AND no in-flight v(N+1).
  // The backend would 409 in either failed case — we surface that as a
  // disabled button + tooltip so the seller sees the reason without a
  // network round-trip. Non-published live rows (draft-only listings
  // pre-first-approval, retired, taken_down) have nothing to republish FROM.
  const canRepublish = !pending && live.status === "published";
  const canDiscard = pending !== null && pending.status === "draft";

  const republishDisabledReason = pending
    ? `Republish blocked — v${pending.version} is ${pending.status}`
    : live.status !== "published"
      ? `Republish blocked — listing is ${live.status}, not Live`
      : undefined;

  return (
    <header className="mb-6 flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h2 className="font-editorial-serif text-3xl text-[#1a1a1a]">
          {live.name}
        </h2>
        <p className="text-[12.5px] text-[#5b5b5b] mt-1">{live.slug}</p>
        <span
          className={`inline-block mt-2 text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full ${
            live.status === "published"
              ? "bg-[#e3efe6] text-[#06402b]"
              : live.status === "review"
                ? "bg-[#fef4e3] text-[#a07520]"
                : live.status === "draft" && live.rejection_notes
                  ? "bg-[#fbe9e7] text-[#8a3a2a]"
                  : "bg-[#f3eee5] text-[#5b5b5b]"
          }`}
        >
          {live.status === "published"
            ? "Live"
            : live.status === "review"
              ? "In review"
              : live.status === "draft" && live.rejection_notes
                ? "Rejected"
                : live.status === "draft"
                  ? "Draft"
                  : live.status}{" "}
          {pendingBadge}
        </span>
      </div>
      <div className="flex flex-col items-end gap-2">
        <button
          type="button"
          onClick={handleRepublish}
          disabled={!canRepublish || busy}
          title={republishDisabledReason}
          className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-4 py-2 rounded-full text-[13px] font-semibold disabled:opacity-50"
        >
          Republish with updates
        </button>
        {canDiscard && (
          <button
            type="button"
            onClick={handleDiscard}
            disabled={busy}
            className="text-[12.5px] font-semibold text-[#8a3a2a] hover:underline"
          >
            Discard pending update
          </button>
        )}
        <a
          href={`/marketplace/listing/${live.slug}`}
          className="text-[12.5px] font-semibold text-[#06402b] hover:underline"
        >
          View on marketplace →
        </a>
      </div>
    </header>
  );
}

function Stats({ live }: { live: Listing }) {
  // install_count isn't on the typed Listing today — the backend includes
  // it on storefront responses but it's optional on the seller-side rows.
  const installs =
    (live as unknown as { install_count?: number }).install_count ?? 0;
  return (
    <div className="flex gap-6 mb-6 text-[13px] text-[#5b5b5b]">
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">
          {installs.toLocaleString()}
        </strong>
        <span>installs</span>
      </div>
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">
          ${(live.price_cents / 100).toFixed(2)}
        </strong>
        <span>price</span>
      </div>
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">
          v{live.version}
        </strong>
        <span>current version</span>
      </div>
    </div>
  );
}

function Tabs({
  tab,
  setTab,
  showPending,
}: {
  tab: Tab;
  setTab: (t: Tab) => void;
  showPending: boolean;
}) {
  return (
    <div role="tablist" className="border-b border-[#e0dbd0] mb-5 flex gap-5">
      <TabBtn name="overview" tab={tab} setTab={setTab}>
        Overview
      </TabBtn>
      <TabBtn name="bundle" tab={tab} setTab={setTab}>
        Bundle
      </TabBtn>
      {showPending && (
        <TabBtn name="pending" tab={tab} setTab={setTab}>
          Pending update
        </TabBtn>
      )}
    </div>
  );
}

function TabBtn({
  name,
  tab,
  setTab,
  children,
}: {
  name: Tab;
  tab: Tab;
  setTab: (t: Tab) => void;
  children: React.ReactNode;
}) {
  const active = tab === name;
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={() => setTab(name)}
      className={`pb-2 text-[13px] font-semibold ${
        active
          ? "text-[#06402b] border-b-2 border-[#06402b]"
          : "text-[#5b5b5b]"
      }`}
    >
      {children}
    </button>
  );
}

function OverviewTab({ listing }: { listing: Listing }) {
  return (
    <div className="space-y-4">
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          Description
        </h3>
        <div className="prose prose-sm max-w-none">
          {listing.description_md}
        </div>
      </section>
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          Tags
        </h3>
        <div className="flex gap-2 flex-wrap">
          {(listing.tags ?? []).map((t) => (
            <span
              key={t}
              className="text-[12px] px-2 py-1 rounded-full bg-[#f3eee5] text-[#5b5b5b]"
            >
              {t}
            </span>
          ))}
        </div>
      </section>
      <section className="grid grid-cols-2 gap-4 text-[13px]">
        <Field label="Category" value={listing.category ?? "—"} />
        <Field label="Format" value={listing.format ?? "—"} />
      </section>
    </div>
  );
}

function BundleTab({ listing }: { listing: Listing }) {
  const mcps = listing.bundle_mcps ?? [];
  const skills = listing.bundle_skills ?? [];
  return (
    <div className="space-y-4">
      <Field label="Snapshotted from agent" value={listing.agent_id ?? "—"} />
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          MCPs bundled
        </h3>
        <ul className="text-[13px] space-y-1">
          {mcps.length === 0 ? (
            <li className="text-[#8a8578] italic">None</li>
          ) : (
            mcps.map((m) => <li key={m}>{m}</li>)
          )}
        </ul>
      </section>
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          Skills bundled
        </h3>
        <ul className="text-[13px] space-y-1">
          {skills.length === 0 ? (
            <li className="text-[#8a8578] italic">None</li>
          ) : (
            skills.map((s) => <li key={s}>{s}</li>)
          )}
        </ul>
      </section>
    </div>
  );
}

function PendingTab({ pending }: { pending: Listing }) {
  const { startEditListing } = useMarketplaceDrawer();
  // Reject flips status back to draft and stamps rejection_notes — that's
  // the only way a draft row carries notes. Use this to switch CTA copy.
  const wasRejected = pending.status === "draft" && !!pending.rejection_notes;
  return (
    <div className="space-y-4">
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          v{pending.version} {wasRejected ? "rejected" : pending.status}
        </h3>
        <p className="text-[13px] text-[#5b5b5b]">
          Created {new Date(pending.created_at).toLocaleDateString()}
        </p>
      </section>

      {wasRejected && (
        <div
          role="status"
          data-testid="rejection-notes"
          className="rounded-xl border border-[#f0b3a8] bg-[#fbe9e7] px-4 py-3 text-[13px] text-[#8a3a2a]"
        >
          <strong className="font-semibold block mb-1">
            Rejection reason
          </strong>
          {pending.rejection_notes}
        </div>
      )}

      <button
        type="button"
        onClick={() => startEditListing(pending)}
        className="bg-[#06402b] hover:bg-[#0a5a3d] text-white px-4 py-2 rounded-full text-[13px] font-semibold"
      >
        {wasRejected ? "Revise & resubmit →" : "Continue editing →"}
      </button>
    </div>
  );
}

function Field({
  label,
  value,
}: {
  label: string;
  value: string | number;
}) {
  return (
    <div>
      <span className="block text-[11.5px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-1">
        {label}
      </span>
      <span className="text-[13px]">{value}</span>
    </div>
  );
}
