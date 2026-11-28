import Link from "next/link";
import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { apiOrigin } from "@/lib/apiOrigin";
import { pageMetadata } from "@/lib/seo";

import { DownloadBlock, OsInvite } from "./OsInvite";
import { freeFor, OsLanding, type SparkPrice } from "./OsLanding";

// ponytail: the landing page is live everywhere; SPARK_SHORT_PAGE=1 brings the short page back. Delete the
// switch and the short page once the founder has settled the design.
const landing = () => process.env.SPARK_SHORT_PAGE !== "1";

export const metadata = pageMetadata({
  title: "Spark, a Voice App for the Mac | Lightbulb",
  description: landing()
    ? "Meet Spark, a one-click voice interface for your Mac. Hold fn, say what you want done, and Spark does it on your Mac."
    : "Meet Spark, a one-click voice interface for your Mac. Download it for Mac with Apple silicon.",
  path: "/os",
});

// The page stays at /os: invite links (/os?ref=CODE) already point here.
// Every line is a VERIFIED row in .agents/product-marketing.md ("Spark").
export default async function OsPage() {
  const [ready, price] = await Promise.all([installerPublished(), sparkPrice()]);
  return landing() ? <OsLanding ready={ready} price={price} /> : <ShortPage ready={ready} price={price} />;
}

// The earlier short page, kept as the fallback behind SPARK_SHORT_PAGE.
function ShortPage({ ready, price }: { ready: boolean; price: SparkPrice }) {
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">Meet Spark</h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          One-click voice interface for your Mac.
        </p>
        <Suspense fallback={<DownloadBlock ready={ready} />}>
          <OsInvite ready={ready} />
        </Suspense>
        {price && (
          <p className="mt-6 text-sm text-[var(--lb-ink-soft)]">
            Free for {freeFor(price)}, then {price.priceText}. Renews monthly until you cancel.
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

// Whether the installer is published yet: the `spark` release page answers 200 once the release exists.
// The page, not the installer file, because replacing the file for a new version deletes it for a few
// seconds. Only a plain 404 (no such release) hides the button: a rate limit, an error or a timeout at
// GitHub must not take the download away. Checked at most every five minutes.
async function installerPublished(): Promise<boolean> {
  try {
    const r = await fetch("https://github.com/TheLightbulbCompany/lightbulb/releases/tag/spark", {
      method: "HEAD",
      redirect: "manual",
      next: { revalidate: 300 },
    });
    return r.status !== 404;
  } catch {
    return true;
  }
}

// Spark's price and trial from the backend (GET /os/config), the same facts the app shows. Any failure or
// unexpected shape is null, and the page shows no price.
async function sparkPrice(): Promise<SparkPrice> {
  try {
    const r = await fetch(`${apiOrigin()}/api/v1/os/config`, { next: { revalidate: 300 } });
    if (!r.ok) return null;
    const c = await r.json();
    return typeof c?.priceText === "string" && Number.isInteger(c?.trialDays) && c.trialDays > 0
      ? {
          priceText: c.priceText,
          trialDays: c.trialDays,
          ...(Number.isInteger(c.trialVoiceMinutes) && c.trialVoiceMinutes > 0 && { trialVoiceMinutes: c.trialVoiceMinutes }),
        }
      : null;
  } catch {
    return null;
  }
}
