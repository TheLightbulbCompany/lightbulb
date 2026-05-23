"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { myListings } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

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
  }, [listingId, userId, getToken]);

  if (!live) {
    return (
      <p className="font-editorial-serif italic text-[#8a8578]">Loading…</p>
    );
  }

  const containerCls =
    mode === "page" ? "max-w-4xl mx-auto px-6 py-8" : "px-2";

  return (
    <div data-mode={mode} className={containerCls}>
      <Header live={live} pending={pending} />
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
}: {
  live: Listing;
  pending: Listing | null;
}) {
  // Sub-badge surfaces an in-flight v(N+1) on the same pill so the seller
  // can tell at a glance whether they have a republish in motion. Task 8
  // wires the Pending tab + action buttons that consume this signal.
  const pendingBadge =
    pending &&
    (pending.status === "review"
      ? `· v${pending.version} in review`
      : pending.rejection_notes
        ? `· v${pending.version} rejected`
        : `· v${pending.version} draft`);
  return (
    <header className="mb-6 flex items-start justify-between gap-3">
      <div>
        <h2 className="font-editorial-serif text-3xl text-[#1a1a1a]">
          {live.name}
        </h2>
        <p className="text-[12.5px] text-[#5b5b5b] mt-1">{live.slug}</p>
      </div>
      <span className="text-[10.5px] tracking-widest uppercase font-bold px-2.5 py-1 rounded-full bg-[#e3efe6] text-[#06402b]">
        Live {pendingBadge}
      </span>
    </header>
  );
}

function Stats({ live }: { live: Listing }) {
  // install_count isn't on the typed Listing today — the backend includes
  // it on storefront responses but it's optional on the seller-side rows.
  // Cast through unknown so the surface degrades gracefully to 0.
  const installs =
    (live as unknown as { install_count?: number }).install_count ?? 0;
  return (
    <div className="flex gap-6 mb-6 text-[13px] text-[#5b5b5b]">
      <div>
        <strong className="block font-editorial-serif text-lg text-[#1a1a1a]">
          {installs}
        </strong>
        <span>{installs} installs</span>
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
  // The manifest fields aren't on the typed Listing today; cast to read
  // them. Future: extend Listing type once we add them to the backend
  // response shape.
  const meta = listing as unknown as {
    agent_id?: string;
    bundle_mcps?: string[];
    bundle_skills?: string[];
  };
  return (
    <div className="space-y-4">
      <Field label="Snapshotted from agent" value={meta.agent_id ?? "—"} />
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          MCPs bundled
        </h3>
        <ul className="text-[13px] space-y-1">
          {(meta.bundle_mcps ?? []).length === 0 ? (
            <li className="text-[#8a8578] italic">None</li>
          ) : (
            meta.bundle_mcps!.map((m) => <li key={m}>{m}</li>)
          )}
        </ul>
      </section>
      <section>
        <h3 className="text-[12px] tracking-widest uppercase font-bold text-[#5b5b5b] mb-2">
          Skills bundled
        </h3>
        <ul className="text-[13px] space-y-1">
          {(meta.bundle_skills ?? []).length === 0 ? (
            <li className="text-[#8a8578] italic">None</li>
          ) : (
            meta.bundle_skills!.map((s) => <li key={s}>{s}</li>)
          )}
        </ul>
      </section>
    </div>
  );
}

function PendingTab({ pending }: { pending: Listing }) {
  return (
    <div>
      <p className="text-[13px] text-[#5b5b5b]">
        Pending tab content placeholder — filled in Task 8.
      </p>
      <p className="text-[13px] text-[#5b5b5b] mt-1">
        v{pending.version} · {pending.status}
      </p>
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
