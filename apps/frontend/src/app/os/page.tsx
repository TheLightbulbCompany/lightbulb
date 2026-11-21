import { Suspense } from "react";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { pageMetadata } from "@/lib/seo";

import { OsInvite } from "./OsInvite";
import { SparkDownload } from "./SparkDownload";

export const metadata = pageMetadata({
  title: "Spark, a Voice App for the Mac | Lightbulb",
  description: "Meet Spark, a one-click voice interface for your Mac. Download it for Mac with Apple silicon.",
  path: "/os",
});

// The page stays at /os: invite links (/os?ref=CODE) already point here.
// Every line is a VERIFIED row in .agents/product-marketing.md ("Spark").
export default function OsPage() {
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">Meet Spark</h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          One-click voice interface for your Mac.
        </p>
        <div className="mt-12">
          <SparkDownload />
        </div>
        <Suspense>
          <OsInvite />
        </Suspense>
        <p className="mt-6 text-sm text-[var(--lb-ink-soft)]">
          For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.
        </p>
      </section>
      <Footer />
    </main>
  );
}
