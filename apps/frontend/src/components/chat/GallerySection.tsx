"use client";

import { Plus } from "lucide-react";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";

export function GallerySection() {
  const { open } = useMarketplaceDrawer();
  return (
    <div className="mt-4 border-t border-[#e0dbd0] pt-3 px-2">
      <div className="font-editorial-serif italic text-[13.5px] text-[#5b5b5b] pb-2 px-1">
        Marketplace
      </div>
      <div className="flex gap-1.5">
        <button
          type="button"
          onClick={() => open({ view: "browse" })}
          className="flex-1 px-2.5 py-2 text-[12.5px] font-medium text-[#1a1a1a] border border-[#e0dbd0] rounded-[10px] hover:border-[#06402b] hover:text-[#06402b] hover:bg-white transition-colors"
        >
          Browse
        </button>
        <button
          type="button"
          onClick={() => open({ view: "publish" })}
          className="flex-1 px-2.5 py-2 text-[12.5px] font-medium text-[#1a1a1a] border border-[#e0dbd0] rounded-[10px] hover:border-[#06402b] hover:text-[#06402b] hover:bg-white transition-colors inline-flex items-center justify-center gap-1.5"
        >
          <Plus className="w-3 h-3" />
          Publish
        </button>
      </div>
    </div>
  );
}
