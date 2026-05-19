"use client";

/**
 * Shared "you can't write to the marketplace" empty state, used in every
 * surface gated by `useMarketplaceWriteGate`. Visual treatment mirrors the
 * settings-page restricted view at apps/frontend/src/app/settings/page.tsx
 * (the "Billing restricted" panel for org members).
 *
 * Pass `title` + `desc` so each surface can give the org member context-
 * appropriate copy ("publishing restricted", "payouts restricted", etc).
 */
export function MarketplaceRestrictedView({
  title,
  desc,
}: {
  title: string;
  desc: string;
}) {
  return (
    <div
      data-testid="marketplace-restricted"
      className="flex flex-col items-center justify-center text-center px-6 py-20 gap-4"
    >
      <svg
        width="40"
        height="40"
        viewBox="0 0 24 24"
        fill="none"
        stroke="#59534d"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
      </svg>
      <div className="text-[18px] font-semibold text-[#1c1917]">{title}</div>
      <div className="text-[14px] text-[#59534d] max-w-[320px]">{desc}</div>
    </div>
  );
}
