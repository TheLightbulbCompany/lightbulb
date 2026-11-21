import Link from "next/link";
import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { pageMetadata } from "@/lib/seo";

import { OsInvite } from "./OsInvite";

export const metadata = pageMetadata({
  title: "Spark, a Voice App for the Mac | Lightbulb",
  description: "Meet Spark, a one-click voice interface for your Mac. Download it for Mac with Apple silicon.",
  path: "/os",
});

// The page stays at /os: invite links (/os?ref=CODE) already point here.
// Every line is a VERIFIED row in .agents/product-marketing.md ("Spark").
export default async function OsPage() {
  const [ready, price] = await Promise.all([installerPublished(), sparkPrice()]);
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">Meet Spark</h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          One-click voice interface for your Mac.
        </p>
        <Suspense>
          <OsInvite ready={ready} />
        </Suspense>
        {price && (
          <p className="mt-6 text-sm text-[var(--lb-ink-soft)]">
            Free for {price.trialDays} days, then {price.priceText}. Renews monthly until you cancel.
          </p>
        )}
        <p className="mt-2 flex gap-4 text-sm text-[var(--lb-ink-soft)]">
          <Link href="/terms" className="underline underline-offset-[3px]">
            Terms
          </Link>
          <Link href="/privacy" className="underline underline-offset-[3px]">
            Privacy
          </Link>
        </p>
        <p className="mt-6 text-sm text-[var(--lb-ink-soft)]">
          For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.
        </p>
      </section>
      <Footer />
    </main>
  );
}

// Whether the installer is published yet: the release asset answers 302 (or 200) once uploaded. Checked
// at most every five minutes, so the button appears without a deploy.
async function installerPublished(): Promise<boolean> {
  try {
    const r = await fetch("https://github.com/TheLightbulbCompany/lightbulb/releases/download/spark/Spark.dmg", {
      method: "HEAD",
      redirect: "manual",
      next: { revalidate: 300 },
    });
    return r.status === 302 || r.status === 200;
  } catch {
    return false;
  }
}

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

// Spark's price and trial from the backend (GET /os/config), the same facts the app shows. Any failure or
// unexpected shape is null, and the page shows no price.
async function sparkPrice(): Promise<{ priceText: string; trialDays: number } | null> {
  try {
    const r = await fetch(`${BACKEND_URL}/os/config`, { next: { revalidate: 300 } });
    if (!r.ok) return null;
    const c = await r.json();
    return typeof c?.priceText === "string" && Number.isInteger(c?.trialDays) && c.trialDays > 0
      ? { priceText: c.priceText, trialDays: c.trialDays }
      : null;
  } catch {
    return null;
  }
}
