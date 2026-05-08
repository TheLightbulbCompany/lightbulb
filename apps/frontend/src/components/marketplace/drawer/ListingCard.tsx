"use client";

import Link from "next/link";
import type { Listing } from "@/lib/marketplace/types";
import { useHire } from "./useHire";

export function ListingCard({ listing, installed }: { listing: Listing; installed: boolean }) {
  const { hire, isHiring } = useHire();
  const tagline =
    listing.description_md.split(/\n{2,}/, 1)[0]?.replace(/[#*_`]/g, "").trim() || "";

  return (
    <article
      className={`relative rounded-2xl border p-5 flex flex-col gap-3 min-h-[200px] transition ${
        installed
          ? "border-[#aac6b4] bg-[#f4f8f4]"
          : "border-[#e0dbd0] bg-white hover:-translate-y-0.5 hover:shadow-md hover:border-[#cfc7b8]"
      }`}
    >
      {installed && (
        <span className="absolute top-3 right-3.5 text-[9.5px] tracking-widest text-[#06402b] font-bold">
          INSTALLED
        </span>
      )}
      <Link
        href={`/marketplace/listing/${listing.slug}`}
        target="_blank"
        rel="noopener noreferrer"
        className="font-editorial-serif text-lg leading-tight text-[#1a1a1a] no-underline hover:underline"
      >
        {listing.name}
      </Link>
      <p className="text-[13px] text-[#5b5b5b] leading-snug flex-1 line-clamp-2">{tagline}</p>
      <div className="flex items-center justify-between pt-2.5 border-t border-dashed border-[#e0dbd0]">
        <span className="text-[11.5px] italic text-[#5b5b5b]">by {listing.seller_id}</span>
        {installed ? (
          <button
            type="button"
            disabled
            className="text-[#06402b] border border-[#06402b] bg-transparent px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold opacity-70"
          >
            Hired ✓
          </button>
        ) : (
          <button
            type="button"
            onClick={() => hire(listing)}
            disabled={isHiring}
            className="bg-[#06402b] text-white px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold hover:bg-[#0a5a3d] disabled:opacity-60"
          >
            Hire
          </button>
        )}
      </div>
    </article>
  );
}
