import { SignedIn, SignedOut, SignInButton, UserButton } from "@clerk/nextjs";
import Link from "next/link";
import type { Metadata } from "next";

import { UserSync } from "@/components/marketplace/storefront/UserSync";

export const metadata: Metadata = {
  title: "Isol8 Marketplace — AI agents you can deploy in one click",
  description: "Browse, buy, and deploy AI agents into your Isol8 container.",
};

export default function MarketplaceLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="editorial-page flex min-h-screen flex-col">
      <UserSync />
      <header className="sticky top-0 z-20 border-b border-[var(--edge)] bg-[rgba(250,247,242,0.92)] backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Link
            href="/marketplace"
            className="font-editorial-serif text-[18px] font-bold uppercase tracking-[0.14em] text-[var(--ink)] no-underline"
          >
            ISOL8
          </Link>
          <nav className="flex items-center gap-1 text-sm">
            <Link
              href="/marketplace/agents"
              className="rounded-full px-3 py-1.5 text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
            >
              Browse
            </Link>
            <SignedIn>
              <Link
                href="/marketplace/buyer"
                className="rounded-full px-3 py-1.5 text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
              >
                Purchases
              </Link>
              <Link
                href="/marketplace/dashboard"
                className="rounded-full px-3 py-1.5 text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
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
      </header>
      <div className="flex-1">{children}</div>
      <footer className="border-t border-[var(--edge)] bg-[var(--cream-deeper)]">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-6 text-xs text-[var(--mute)]">
          <span className="font-mono uppercase tracking-[0.14em]">Isol8 marketplace · v0</span>
          <nav className="flex items-center gap-4">
            <Link href="/terms" className="text-[var(--mute)] no-underline hover:text-[var(--forest)]">
              Terms
            </Link>
            <Link href="/privacy" className="text-[var(--mute)] no-underline hover:text-[var(--forest)]">
              Privacy
            </Link>
            <Link href="/" className="text-[var(--mute)] no-underline hover:text-[var(--forest)]">
              ← Back to Isol8
            </Link>
          </nav>
          <span>Free to install. Usage costs follow your provider settings.</span>
        </div>
      </footer>
    </div>
  );
}
