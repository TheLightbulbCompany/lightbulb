"use client";

import Link from "next/link";
import { Dialog as DialogPrimitive } from "radix-ui";
import { ChevronLeft, X, Plus, ExternalLink } from "lucide-react";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import { BrowseView } from "./BrowseView";
import { MyListingsView } from "./MyListingsView";
import { PublishWizard } from "./PublishWizard";
import { ListingDetailView } from "../ListingDetailView";

export function MarketplaceDrawer() {
  const { state, close, open } = useMarketplaceDrawer();

  if (!state.isOpen) return null;

  const isPublish = state.view === "publish";
  const isDetail = state.view === "detail";
  // Both publish and detail are focused views: hide the "+ Publish" /
  // "Open in full page" action cluster so the user isn't tempted to jump
  // away mid-task.
  const showActions = !isPublish && !isDetail;

  return (
    <DialogPrimitive.Root open onOpenChange={(o) => !o && close()}>
      <DialogPrimitive.Portal>
      {/* Scrim */}
      <DialogPrimitive.Overlay
        data-modal-overlay=""
        className="fixed inset-0 z-(--z-overlay) bg-[var(--midnight)]/45 transition-opacity duration-(--dur-slow)"
      />
      <DialogPrimitive.Content
        aria-describedby={undefined}
        data-mode={isPublish ? "publish" : undefined}
        className="fixed top-0 right-0 z-[60] h-screen w-[min(920px,98vw)] bg-[var(--cream)] border-l border-[var(--edge)] shadow-xl flex flex-col transform translate-x-0 transition-transform duration-(--dur-slow)"
        aria-label="Marketplace drawer"
      >
        {/* HEADER */}
        <div className="px-6 py-3.5 border-b border-[var(--edge)] bg-[var(--cream)] flex items-center justify-between gap-3.5">
          {isPublish ? <PublishHeader /> : isDetail ? <DetailHeader /> : <DefaultHeader />}
          <div className="flex items-center gap-2.5 shrink-0">
            {showActions && (
              <>
                <button
                  type="button"
                  onClick={() => open({ view: "publish" })}
                  className="bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-[var(--cream)] rounded-full px-4 py-2 text-ui font-semibold inline-flex items-center gap-1.5"
                >
                  <Plus className="w-3 h-3" />
                  Publish your agent
                </button>
                <Link
                  href="/marketplace"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-ui text-muted-foreground hover:text-[var(--midnight)] inline-flex items-center gap-1"
                >
                  Open in full page <ExternalLink className="w-3 h-3" />
                </Link>
              </>
            )}
            <button
              type="button"
              onClick={close}
              aria-label="Close drawer"
              className="w-8 h-8 rounded-full border border-[var(--edge)] bg-card text-muted-foreground hover:text-[var(--midnight)] flex items-center justify-center"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* BODY */}
        <div className="flex-1 overflow-y-auto px-7 pt-6 pb-20">
          {state.view === "browse" && <BrowseView />}
          {state.view === "mine" && <MyListingsView />}
          {state.view === "publish" && <PublishWizard />}
          {state.view === "detail" && <DetailBody />}
        </div>
      </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

function DefaultHeader() {
  const { state, setView } = useMarketplaceDrawer();
  return (
    <div className="flex items-center gap-4 min-w-0">
      <span className="font-editorial-serif italic text-ui text-muted-foreground">Marketplace</span>
      <div className="bg-cream-deep rounded-full p-1 flex gap-1">
        <button
          type="button"
          onClick={() => setView("browse")}
          className={`px-4 py-1.5 rounded-full text-ui font-medium ${
            state.view === "browse"
              ? "bg-card text-[var(--midnight)] shadow-sm"
              : "text-muted-foreground"
          }`}
        >
          Browse
        </button>
        <button
          type="button"
          onClick={() => setView("mine")}
          className={`px-4 py-1.5 rounded-full text-ui font-medium ${
            state.view === "mine"
              ? "bg-card text-[var(--midnight)] shadow-sm"
              : "text-muted-foreground"
          }`}
        >
          My listings
        </button>
      </div>
    </div>
  );
}

function PublishHeader() {
  const { state, setView } = useMarketplaceDrawer();
  const previous = state.prevView ?? "browse";
  const isEditing = !!state.editListing;
  return (
    <div className="flex items-center gap-3.5 min-w-0">
      <button
        type="button"
        onClick={() => setView(previous)}
        className="inline-flex items-center gap-1.5 text-ui text-muted-foreground hover:text-[var(--midnight)] hover:bg-cream-deep rounded-full px-2.5 py-1.5"
      >
        <ChevronLeft className="w-3 h-3" />
        Marketplace
      </button>
      <span className="font-editorial-serif italic text-sm text-muted-foreground">
        {isEditing ? `Edit "${state.editListing?.name}"` : "Publish an agent"}
      </span>
    </div>
  );
}

function DetailHeader() {
  return (
    <div className="flex items-center gap-3.5 min-w-0">
      <span className="font-editorial-serif italic text-ui text-muted-foreground">Marketplace</span>
      <span className="font-editorial-serif italic text-sm text-muted-foreground">Listing details</span>
    </div>
  );
}

function DetailBody() {
  const { state, setView } = useMarketplaceDrawer();
  // detailListingId is required to render — guard against the (provider-bug)
  // case where view flipped to "detail" without an id being set.
  if (!state.detailListingId) return null;
  return (
    <div>
      <button
        type="button"
        onClick={() => setView("mine")}
        className="text-ui font-semibold text-[var(--midnight)] hover:underline mb-3"
      >
        ← Back to my listings
      </button>
      <ListingDetailView listingId={state.detailListingId} mode="drawer" />
    </div>
  );
}
