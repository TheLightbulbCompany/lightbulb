import { SignedIn, SignedOut, SignInButton, UserButton } from "@clerk/nextjs";
import Link from "next/link";
import type { Metadata } from "next";

import { Navbar } from "@/components/landing/Navbar";
import { UserSync } from "@/components/marketplace/storefront/UserSync";

export const metadata: Metadata = {
  title: "Lightbulb Marketplace — AI agents you can deploy in one click",
  description: "Browse, buy, and deploy AI agents into your Lightbulb container.",
};

export default function MarketplaceLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="editorial-page flex min-h-screen flex-col pt-[65px]">
      <UserSync />
      <Navbar />
      <div className="marketplace-subnav sticky top-[65px] z-10 border-b border-[var(--edge)] bg-[var(--cream)]/92 backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3 text-sm">
          <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--midnight-muted)]">
            Marketplace
          </div>
          <nav className="flex items-center gap-1">
            <Link
              href="/marketplace"
              className="rounded-full px-3 py-1.5 text-[var(--midnight-soft)] no-underline hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            >
              Featured
            </Link>
            <Link
              href="/marketplace/agents"
              className="rounded-full px-3 py-1.5 text-[var(--midnight-soft)] no-underline hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            >
              Browse
            </Link>
            <Link
              href="/marketplace/companies"
              className="rounded-full px-3 py-1.5 text-[var(--midnight-soft)] no-underline hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            >
              Companies
            </Link>
            <SignedIn>
              <Link
                href="/marketplace/buyer"
                className="rounded-full px-3 py-1.5 text-[var(--midnight-soft)] no-underline hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
              >
                Purchases
              </Link>
              <Link
                href="/marketplace/dashboard"
                className="rounded-full px-3 py-1.5 text-[var(--midnight-soft)] no-underline hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
              >
                Earnings
              </Link>
              <span className="ml-2">
                <UserButton afterSignOutUrl="/marketplace" />
              </span>
            </SignedIn>
            <SignedOut>
              <SignInButton mode="modal">
                <button className="editorial-btn editorial-btn--sm editorial-btn--primary">
                  Sign in
                </button>
              </SignInButton>
            </SignedOut>
          </nav>
        </div>
      </div>
      <div className="flex-1">{children}</div>
      <footer className="border-t border-[var(--edge)] bg-[var(--cream-deep)]">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-6 text-xs text-[var(--midnight-muted)]">
          <span className="font-mono uppercase tracking-[0.14em]">Lightbulb marketplace · v0</span>
          <nav className="flex items-center gap-4">
            <Link href="/terms" className="text-[var(--midnight-muted)] no-underline hover:text-[var(--midnight)]">
              Terms
            </Link>
            <Link href="/privacy" className="text-[var(--midnight-muted)] no-underline hover:text-[var(--midnight)]">
              Privacy
            </Link>
            <Link href="/" className="text-[var(--midnight-muted)] no-underline hover:text-[var(--midnight)]">
              ← Back to Lightbulb
            </Link>
          </nav>
          <span>Free to install. Usage costs follow your provider settings.</span>
        </div>
      </footer>
    </div>
  );
}
