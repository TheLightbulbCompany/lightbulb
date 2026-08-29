"use client";

import { Plus } from "lucide-react";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

export function GallerySection() {
  const { open } = useMarketplaceDrawer();
  return (
    <div className="mt-4 border-t border-[var(--edge)] pt-3 px-2">
      <div className="font-editorial-serif italic text-ui text-[var(--midnight-muted)] pb-2 px-1">
        Marketplace
      </div>
      <div className="flex gap-1.5">
        <button
          type="button"
          onClick={() => open({ view: "browse" })}
          className="flex-1 px-2.5 py-2 text-ui font-medium text-[var(--midnight)] border border-[var(--edge)] rounded-md hover:border-[var(--midnight)] hover:text-[var(--midnight)] hover:bg-cream transition-colors"
        >
          Browse
        </button>
        <button
          type="button"
          onClick={() => open({ view: "publish" })}
          className="flex-1 px-2.5 py-2 text-ui font-medium text-[var(--midnight)] border border-[var(--edge)] rounded-md hover:border-[var(--midnight)] hover:text-[var(--midnight)] hover:bg-cream transition-colors inline-flex items-center justify-center gap-1.5"
        >
          <Plus className="w-3 h-3" />
          Publish
        </button>
      </div>
    </div>
  );
}
