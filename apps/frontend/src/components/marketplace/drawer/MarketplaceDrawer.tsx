"use client";

import Link from "next/link";
import { ChevronLeft, X, Plus, ExternalLink } from "lucide-react";

import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import { BrowseView } from "./BrowseView";
import { MyListingsView } from "./MyListingsView";
import { PublishWizard } from "./PublishWizard";

export function MarketplaceDrawer() {
  const { state, close, open } = useMarketplaceDrawer();

  if (!state.isOpen) return null;

  const isPublish = state.view === "publish";

  return (
    <>
      {/* Scrim */}
      <div
        onClick={close}
        className="fixed inset-0 z-50 bg-[rgba(20,30,25,0.45)] transition-opacity duration-250"
        aria-hidden="true"
      />
      <aside
        data-mode={isPublish ? "publish" : undefined}
        className="fixed top-0 right-0 z-[60] h-screen w-[min(920px,98vw)] bg-[#faf7f2] border-l border-[#e0dbd0] shadow-[-30px_0_80px_rgba(10,30,20,0.16)] flex flex-col transform translate-x-0 transition-transform duration-350"
        aria-label="Marketplace drawer"
      >
        {/* HEADER */}
        <div className="px-6 py-3.5 border-b border-[#e0dbd0] bg-[#faf7f2] flex items-center justify-between gap-3.5">
          {isPublish ? <PublishHeader /> : <DefaultHeader />}
          <div className="flex items-center gap-2.5 flex-shrink-0">
            {!isPublish && (
              <>
                <button
                  type="button"
                  onClick={() => open({ view: "publish" })}
                  className="bg-[#06402b] hover:bg-[#0a5a3d] text-white rounded-full px-4 py-2 text-[13px] font-semibold inline-flex items-center gap-1.5"
                >
                  <Plus className="w-3.5 h-3.5" />
                  Publish your agent
                </button>
                <Link
                  href="/marketplace"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[12.5px] text-[#5b5b5b] hover:text-[#1a1a1a] inline-flex items-center gap-1"
                >
                  Open in full page <ExternalLink className="w-3 h-3" />
                </Link>
              </>
            )}
            <button
              type="button"
              onClick={close}
              aria-label="Close drawer"
              className="w-8 h-8 rounded-full border border-[#e0dbd0] bg-white text-[#5b5b5b] hover:text-[#1a1a1a] flex items-center justify-center"
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
        </div>
      </aside>
    </>
  );
}

function DefaultHeader() {
  const { state, setView } = useMarketplaceDrawer();
  return (
    <div className="flex items-center gap-4 min-w-0">
      <span className="font-editorial-serif italic text-[13px] text-[#5b5b5b]">Marketplace</span>
      <div className="bg-[#f3eee5] rounded-full p-1 flex gap-1">
        <button
          type="button"
          onClick={() => setView("browse")}
          className={`px-4 py-1.5 rounded-full text-[13px] font-medium ${
            state.view === "browse"
              ? "bg-white text-[#1a1a1a] shadow-sm"
              : "text-[#5b5b5b]"
          }`}
        >
          Browse
        </button>
        <button
          type="button"
          onClick={() => setView("mine")}
          className={`px-4 py-1.5 rounded-full text-[13px] font-medium ${
            state.view === "mine"
              ? "bg-white text-[#1a1a1a] shadow-sm"
              : "text-[#5b5b5b]"
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
  return (
    <div className="flex items-center gap-3.5 min-w-0">
      <button
        type="button"
        onClick={() => setView(previous)}
        className="inline-flex items-center gap-1.5 text-[13px] text-[#5b5b5b] hover:text-[#1a1a1a] hover:bg-[#f3eee5] rounded-full px-2.5 py-1.5"
      >
        <ChevronLeft className="w-3.5 h-3.5" />
        Marketplace
      </button>
      <span className="font-editorial-serif italic text-[14px] text-[#5b5b5b]">
        Publish an agent
      </span>
    </div>
  );
}
