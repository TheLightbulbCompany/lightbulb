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
import { notifyApiError } from "@/lib/notify";

type Tab = "overview" | "bundle" | "pending" | "history";

interface Props {
  listingId: string;
  mode: "drawer" | "page";
}

type LoadState = "loading" | "loaded" | "not-found";

export function ListingDetailView({ listingId, mode }: Props) {
  const { isLoaded, userId, getToken } = useAuth();
  const [live, setLive] = useState<Listing | null>(null);
  const [pending, setPending] = useState<Listing | null>(null);
  // Every version row for this listing (published + retired predecessors +
  // any in-flight draft/review), newest first — backs the History tab so all
  // versions live in one place.
  const [versions, setVersions] = useState<Listing[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  // Bump to force the fetch effect to re-run after a mutation (discard).
  // Republish hands off to the wizard so doesn't need a local refetch.
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    // Clerk still resolving — leave loadState as "loading" so we don't
    // flash a terminal state during initial hydration.
    if (!isLoaded) return;
    // Auth resolved but no user, or token issuance failed (transient
    // Clerk issue / mid-session expiry). Fold into the same terminal
    // state as the rejected-fetch path — otherwise the view sits on
    // Loading… forever.
    if (!userId) {
      // Synchronizing Clerk's resolved auth state into our terminal view
      // state — the external-system→React sync the rule explicitly allows.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setLoadState("not-found");
      return;
    }
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!alive) return;
      if (!jwt) {
        setLoadState("not-found");
        return;
      }
      // Fetch this seller's listings; find the rows for our listing_id. One
      // round-trip surfaces both the live row and any in-flight v(N+1)
      // draft/review — we don't need a per-row GET for either.
      let items: Listing[];
      try {
        ({ items } = await myListings({ sellerId: userId, jwt }));
      } catch {
        // Network / 5xx / expired-token. Fold the failure into the same
        // terminal state as the empty-rows case so the view exits the
        // spinner instead of stranding the seller.
        if (!alive) return;
        setLoadState("not-found");
        return;
      }
      if (!alive) return;
      const rows = items.filter((l) => l.listing_id === listingId);
      if (rows.length === 0) {
        // Seller's list doesn't include this listing — bad listing_id,
        // ownership changed, or a pagination/limit gap on /me/listings.
        // Render a terminal error state instead of indefinite Loading…
        setLoadState("not-found");
        return;
      }
      // Keep the full version set (newest first) for the History tab.
      setVersions([...rows].sort((a, b) => b.version - a.version));
      const publishedRow = rows.find((r) => r.status === "published") ?? null;
      const nonTerminalRow =
        rows.find((r) => r.status === "draft" || r.status === "review") ?? null;
      // When there's no published row yet (brand-new draft or first-time
      // submission in review), promote the non-terminal row into the
      // `live` slot so the detail view renders SOMETHING. This mirrors
      // the same promotion MyListingsView does — keeps the "is this row
      // a pending update?" semantics tight: pending only fires when
      // there's a distinct live + non-terminal pair (i.e. a true
      // republish in flight), not for a listing's base v=1 draft.
      if (publishedRow) {
        setLive(publishedRow);
        setPending(nonTerminalRow);
      } else {
        setLive(nonTerminalRow ?? rows[0] ?? null);
        setPending(null);
      }
      setLoadState("loaded");
    })();
    return () => {
      alive = false;
    };
  }, [listingId, isLoaded, userId, getToken, refreshKey]);

  // When the pending row disappears (discarded, or admin approved/rejected),
  // bounce the user off the now-hidden Pending tab so they don't land on an
  // empty pane.
  useEffect(() => {
    // Reacting to the server-derived `pending` row vanishing — an
    // external-data→React sync, which the rule sanctions.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (tab === "pending" && !pending) setTab("overview");
    // History tab only shows with >1 version; bounce off if a discard
    // collapsed the listing back to a single version.
    if (tab === "history" && versions.length <= 1) setTab("overview");
  }, [pending, tab, versions.length]);

  if (loadState === "not-found") {
    return (
      <p
        role="status"
        className="font-editorial-serif italic text-[var(--err)] p-4"
      >
        We couldn&apos;t find this listing in your account. It may have been
        retired, taken down, or transferred to another owner.
      </p>
    );
  }

  if (!live) {
    return (
      <p className="font-editorial-serif italic text-[var(--midnight-muted)]">Loading…</p>
    );
  }

  const containerCls =
    mode === "page" ? "max-w-4xl mx-auto px-6 py-8" : "px-2";

  return (
    <div data-mode={mode} className={containerCls}>
      <Header
        live={live}
        pending={pending}
        mode={mode}
        onPendingChange={() => setRefreshKey((k) => k + 1)}
      />
      <Stats live={live} />
      <Tabs
        tab={tab}
        setTab={setTab}
        showPending={!!pending}
        showHistory={versions.length > 1}
      />
      {tab === "overview" && <OverviewTab listing={live} />}
      {tab === "bundle" && <BundleTab listing={live} />}
      {tab === "pending" && pending && <PendingTab pending={pending} />}
      {tab === "history" && (
        <VersionHistoryTab versions={versions} liveVersion={live.version} />
      )}
    </div>
  );
}

function Header({
  live,
  pending,
  mode,
  onPendingChange,
}: {
  live: Listing;
  pending: Listing | null;
  mode: "drawer" | "page";
  onPendingChange: () => void;
}) {
  const { getToken } = useAuth();
  const { startEditListing, setView } = useMarketplaceDrawer();
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
    } catch (err) {
      notifyApiError(err, "Couldn't start the republish");
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
    } catch (err) {
      notifyApiError(err, "Couldn't discard the pending version");
    } finally {
      setBusy(false);
    }
  }

  // A listing that never went live is just its draft: deleting it removes the
  // listing, so leave this view for the seller's list afterwards.
  async function handleDeleteDraft() {
    if (!window.confirm(`Delete the draft "${live.name}"? This can't be undone.`)) return;
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) return;
      await discardListingVersion({ listingId: live.listing_id, version: live.version, jwt });
      if (mode === "drawer") setView("mine");
      else window.location.assign("/marketplace");
    } finally {
      setBusy(false);
    }
  }

  // Republish requires a Live (published) row AND no in-flight v(N+1).
  // The backend would 409 in either failed case — we surface that as a
  // disabled button + tooltip so the seller sees the reason without a
  // network round-trip. Non-published live rows (draft-only listings
  // pre-first-approval, retired, taken_down) have nothing to republish
  // FROM — for draft/review listings we surface a Continue/Resubmit CTA
  // instead so the seller can finish their first publish.
  const canRepublish = !pending && live.status === "published";
  const canDiscard = pending !== null && pending.status === "draft";
  // A draft/review/rejected `live` row means this listing was promoted up
  // from `pending` because no published version exists yet. The Pending
  // tab is hidden in that case, so the wizard is otherwise unreachable.
  const showContinueEdit =
    live.status === "draft" || live.status === "review";
  const continueEditLabel = live.rejection_notes
    ? "Revise & resubmit →"
    : live.status === "review"
      ? "Edit draft →"
      : "Continue editing →";

  async function handleContinueEdit() {
    setBusy(true);
    try {
      startEditListing(live);
    } finally {
      setBusy(false);
    }
  }

  const republishDisabledReason = pending
    ? `Republish blocked — v${pending.version} is ${pending.status}`
    : live.status !== "published"
      ? `Republish blocked — listing is ${live.status}, not Live`
      : undefined;

  return (
    <header className="mb-6 flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h2 className="font-editorial-serif text-2xl text-[var(--midnight)]">
          {live.name}
        </h2>
        <p className="text-ui text-muted-foreground mt-1">{live.slug}</p>
        <span
          className={`inline-block mt-2 text-2xs tracking-caps uppercase font-bold px-2.5 py-1 rounded-full ${
            live.status === "published"
              ? "border border-[var(--ok)] text-[var(--ok)]"
              : live.status === "review"
                ? "border border-[var(--warn)] text-[var(--warn)]"
                : live.status === "draft" && live.rejection_notes
                  ? "border border-[var(--err)] text-[var(--err)]"
                  : "border border-[var(--edge)] text-muted-foreground"
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
        {showContinueEdit ? (
          <button
            type="button"
            onClick={handleContinueEdit}
            disabled={busy}
            className="bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-[var(--cream)] px-4 py-2 rounded-full text-ui font-semibold disabled:opacity-50"
          >
            {continueEditLabel}
          </button>
        ) : (
          <button
            type="button"
            onClick={handleRepublish}
            disabled={!canRepublish || busy}
            title={republishDisabledReason}
            className="bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-[var(--cream)] px-4 py-2 rounded-full text-ui font-semibold disabled:opacity-50"
          >
            Republish with updates
          </button>
        )}
        {canDiscard && (
          <button
            type="button"
            onClick={handleDiscard}
            disabled={busy}
            className="text-ui font-semibold text-[var(--err)] hover:underline"
          >
            Discard pending update
          </button>
        )}
        {live.status === "draft" && (
          <button
            type="button"
            onClick={handleDeleteDraft}
            disabled={busy}
            className="text-ui font-semibold text-[var(--err)] hover:underline"
          >
            Delete draft
          </button>
        )}
        {/* Only a published listing has a public page; a draft's link 404s. */}
        {live.status === "published" && (
          <a
            href={`/marketplace/listing/${live.listing_id}`}
            className="text-ui font-semibold text-[var(--midnight)] hover:underline"
          >
            View on marketplace →
          </a>
        )}
      </div>
    </header>
  );
}

function Stats({ live }: { live: Listing }) {
  const installs = live.install_count ?? 0;
  return (
    <div className="flex gap-6 mb-6 text-ui text-muted-foreground">
      <div>
        <strong className="block font-editorial-serif text-lg text-[var(--midnight)]">
          {installs.toLocaleString()}
        </strong>
        <span>installs</span>
      </div>
      <div>
        <strong className="block font-editorial-serif text-lg text-[var(--midnight)]">
          ${(live.price_cents / 100).toFixed(2)}
        </strong>
        <span>price</span>
      </div>
      <div>
        <strong className="block font-editorial-serif text-lg text-[var(--midnight)]">
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
  showHistory,
}: {
  tab: Tab;
  setTab: (t: Tab) => void;
  showPending: boolean;
  showHistory: boolean;
}) {
  return (
    <div role="tablist" className="border-b border-[var(--edge)] mb-5 flex gap-5">
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
      {showHistory && (
        <TabBtn name="history" tab={tab} setTab={setTab}>
          Version history
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
      className={`pb-2 text-ui font-semibold ${
        active
          ? "text-[var(--midnight)] border-b-2 border-[var(--midnight)]"
          : "text-muted-foreground"
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
        <h3 className="text-xs tracking-caps uppercase font-bold text-[var(--midnight-label)] mb-2">
          Description
        </h3>
        <div className="prose prose-sm max-w-none">
          {listing.description_md}
        </div>
      </section>
      <section>
        <h3 className="text-xs tracking-caps uppercase font-bold text-[var(--midnight-label)] mb-2">
          Tags
        </h3>
        <div className="flex gap-2 flex-wrap">
          {(listing.tags ?? []).map((t) => (
            <span
              key={t}
              className="text-xs px-2 py-1 rounded-full bg-cream-deep text-muted-foreground"
            >
              {t}
            </span>
          ))}
        </div>
      </section>
      <section className="grid grid-cols-2 gap-4 text-ui">
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
        <h3 className="text-xs tracking-caps uppercase font-bold text-[var(--midnight-label)] mb-2">
          MCPs bundled
        </h3>
        <ul className="text-ui space-y-1">
          {mcps.length === 0 ? (
            <li className="text-[var(--midnight-muted)] italic">None</li>
          ) : (
            mcps.map((m) => <li key={m}>{m}</li>)
          )}
        </ul>
      </section>
      <section>
        <h3 className="text-xs tracking-caps uppercase font-bold text-[var(--midnight-label)] mb-2">
          Skills bundled
        </h3>
        <ul className="text-ui space-y-1">
          {skills.length === 0 ? (
            <li className="text-[var(--midnight-muted)] italic">None</li>
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
        <h3 className="text-xs tracking-caps uppercase font-bold text-[var(--midnight-label)] mb-2">
          v{pending.version} {wasRejected ? "rejected" : pending.status}
        </h3>
        <p className="text-ui text-muted-foreground">
          Created {new Date(pending.created_at).toLocaleDateString()}
        </p>
      </section>

      {wasRejected && (
        <div
          role="status"
          data-testid="rejection-notes"
          className="rounded-xl border border-[var(--err)] px-4 py-3 text-ui text-[var(--err)]"
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
        className="bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-[var(--cream)] px-4 py-2 rounded-full text-ui font-semibold"
      >
        {wasRejected ? "Revise & resubmit →" : "Continue editing →"}
      </button>
    </div>
  );
}

function versionStatusLabel(l: Listing): string {
  switch (l.status) {
    case "published":
      return "Live";
    case "review":
      return "In review";
    case "draft":
      return l.rejection_notes ? "Rejected" : "Draft";
    case "retired":
      return "Retired";
    case "taken_down":
      return "Taken down";
    default:
      return l.status;
  }
}

function versionStatusCls(status: string): string {
  switch (status) {
    case "published":
      return "border border-[var(--ok)] text-[var(--ok)]";
    case "review":
      return "border border-[var(--warn)] text-[var(--warn)]";
    case "draft":
      return "border border-[var(--edge)] text-muted-foreground";
    case "taken_down":
      return "border border-[var(--err)] text-[var(--err)]";
    default:
      // retired + any future status — neutral grey
      return "border border-[var(--edge)] text-[var(--midnight-muted)]";
  }
}

function VersionHistoryTab({
  versions,
  liveVersion,
}: {
  versions: Listing[];
  liveVersion: number;
}) {
  return (
    <ul data-testid="version-history" className="space-y-2">
      {versions.map((v) => {
        const installs = v.install_count ?? 0;
        const stamp = v.published_at ?? v.created_at;
        return (
          <li
            key={v.version}
            data-testid={`version-row-${v.version}`}
            className={`flex items-center justify-between gap-3 rounded-xl border px-4 py-3 ${
              v.version === liveVersion
                ? "border-[var(--ok)]"
                : "border-[var(--edge)] bg-card"
            }`}
          >
            <div className="flex items-center gap-3">
              <span className="font-editorial-serif text-base text-[var(--midnight)]">
                v{v.version}
              </span>
              <span
                className={`text-2xs tracking-caps uppercase font-bold px-2 py-0.5 rounded-full ${versionStatusCls(
                  v.status,
                )}`}
              >
                {versionStatusLabel(v)}
              </span>
            </div>
            <div className="flex items-center gap-5 text-ui text-muted-foreground">
              <span>{installs.toLocaleString()} installs</span>
              <span>{stamp ? new Date(stamp).toLocaleDateString() : "—"}</span>
            </div>
          </li>
        );
      })}
    </ul>
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
      <span className="block label-caps mb-1">
        {label}
      </span>
      <span className="text-ui">{value}</span>
    </div>
  );
}
